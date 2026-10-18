// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/board.h"
#include "core/fx.h"
#include "core/panel.h"
#include <alsa/asoundlib.h>
#include <array>
#include <fstream>
#include <sstream>
#include <iostream>
#include <vector>
#include <filesystem>
#include <memory>
#include <algorithm>
#include <atomic>
#include <csignal>
#include <sys/prctl.h>
#include <chrono>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>

static std::vector<uint8_t> read_rom(const std::filesystem::path &path, size_t size) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("Cannot open " + path.string());
    std::vector<uint8_t> data((std::istreambuf_iterator<char>(f)), {});
    if (data.size() != size) throw std::runtime_error("Wrong ROM size: " + path.string());
    return data;
}

static Board load_board(const std::filesystem::path &romdir) {
    return Board(read_rom(romdir / "v1.2.u15", 32768), read_rom(romdir / "v1.2.u28", 524288));
}

// The offline runner's end-of-run dumps: DSP memories, coverage, the controller
// bus counts and RAM, and a one-line summary.
static void dump_board(Board &board, const std::filesystem::path &out) {
    std::ofstream pm(out/"dsp-pm.bin",std::ios::binary), dm(out/"dsp-dm.bin",std::ios::binary);
    std::ofstream stats(out/"coverage.tsv"), bus(out/"mcu-bus.tsv"), mem(out/"mcu-ram.bin",std::ios::binary);
    if (!pm || !dm || !stats || !bus || !mem) throw std::runtime_error("Cannot write dumps");
    adsp2181_t *dsp=board.dsp.get();
    for (unsigned a=0;a<0x4000;++a) {
        uint32_t p=adsp2181_pm(dsp)[a];
        pm.put(p&255); pm.put((p>>8)&255); pm.put((p>>16)&255);
        uint16_t d=adsp2181_dm(dsp)[a]; dm.put(d&255); dm.put(d>>8);
        auto n=adsp2181_coverage_count(dsp,a);
        if(n) stats << "dsp\t" << std::hex << a << '\t' << std::dec << n << '\n';
    }
    for(unsigned a=0;a<65536;++a) {
        if(board.coverage[a]) stats << "mcu\t" << std::hex << a << '\t' << std::dec << board.coverage[a] << '\n';
        if(board.reads[a]||board.writes[a]) bus << std::hex << a << '\t' << unsigned(board.ram[a]) << '\t' << std::dec << board.reads[a] << '\t' << board.writes[a] << '\n';
    }
    mem.write(reinterpret_cast<const char*>(board.ram.data()),board.ram.size());
    std::cout << "DSP PC=0x" << std::hex << adsp2181_pc(dsp)
        << " idle=" << adsp2181_idle(dsp) << " IMASK=0x" << adsp2181_imask(dsp)
        << " MCU A=0x" << board.mcu.m_cpu[0].irp << " B=0x" << board.mcu.m_cpu[1].irp
        << std::dec << " BDMA transfers=" << board.transfers << " IDMA writes=" << board.idma_writes
        << " timer IRQs=" << board.timer_irqs << " SPORT words=" << board.tx_words << '\n';
}


static std::atomic_bool realtime_running=true;
static void stop_realtime(int) { realtime_running=false; }

// A local datagram endpoint lets the desktop panel inject the same raw MIDI
// bytes that arrive from ALSA. It deliberately stays separate from ALSA so a
// panel gesture cannot depend on a DAW connection or an exposed MIDI port.
struct LocalMidiControl {
    struct ClockDatagram {
        sockaddr_un destination{};
        socklen_t destination_size=0;
        uint32_t sequence=0;
        uint8_t status=0;
    };
    static constexpr size_t clock_capacity=1024;
    int fd=-1;
    std::string path;
    sockaddr_un clock_destination{}; // written by the engine thread, copied into the SPSC queue
    socklen_t clock_destination_size=0;
    uint32_t next_clock_sequence=0;
    std::array<ClockDatagram,clock_capacity> clock_queue{};
    std::atomic<uint64_t> clock_head{0}, clock_tail{0}, clock_dropped{0};
    std::atomic_bool clock_stopping{false};
    std::thread clock_thread;
    ~LocalMidiControl() {
        clock_stopping.store(true,std::memory_order_release);
        if(clock_thread.joinable()) clock_thread.join();
        if(fd>=0) close(fd);
        if(!path.empty()) unlink(path.c_str());
    }
    void open(const std::string &requested_path) {
        if(requested_path.empty()) return;
        if(requested_path.size() >= sizeof(sockaddr_un::sun_path))
            throw std::runtime_error("Control socket path is too long");
        path=requested_path;
        fd=socket(AF_UNIX,SOCK_DGRAM,0);
        if(fd<0) throw std::runtime_error("Cannot create control socket: "+std::string(std::strerror(errno)));
        unlink(path.c_str());
        sockaddr_un address{};
        address.sun_family=AF_UNIX;
        std::strncpy(address.sun_path,path.c_str(),sizeof(address.sun_path)-1);
        if(bind(fd,reinterpret_cast<sockaddr*>(&address),sizeof(address))<0)
            throw std::runtime_error("Cannot bind control socket '"+path+"': "+std::string(std::strerror(errno)));
        if(fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)|O_NONBLOCK)<0)
            throw std::runtime_error("Cannot set control socket nonblocking: "+std::string(std::strerror(errno)));
        chmod(path.c_str(),0600);
        clock_thread=std::thread([this] {
            while(!clock_stopping.load(std::memory_order_acquire) ||
                  clock_head.load(std::memory_order_relaxed)!=clock_tail.load(std::memory_order_acquire)) {
                const uint64_t head=clock_head.load(std::memory_order_relaxed);
                if(head==clock_tail.load(std::memory_order_acquire)) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    continue;
                }
                const ClockDatagram event=clock_queue[head&(clock_capacity-1)];
                std::array<uint8_t,9> packet{0xf0,0x7d,'K',uint8_t(event.sequence>>24),
                    uint8_t(event.sequence>>16),uint8_t(event.sequence>>8),uint8_t(event.sequence),
                    event.status,0xf7};
                if(sendto(fd,packet.data(),packet.size(),0,
                          reinterpret_cast<const sockaddr*>(&event.destination),event.destination_size)<0)
                    clock_dropped.fetch_add(1,std::memory_order_relaxed);
                clock_head.store(head+1,std::memory_order_release);
            }
        });
    }
    void forward_clock(uint8_t status) {
        if(!is_transport_realtime(status) || clock_destination_size==0) return;
        const uint32_t sequence=next_clock_sequence++;
        const uint64_t tail=clock_tail.load(std::memory_order_relaxed);
        if(tail-clock_head.load(std::memory_order_acquire)>=clock_capacity) {
            clock_dropped.fetch_add(1,std::memory_order_relaxed);
            return;
        }
        auto &slot=clock_queue[tail&(clock_capacity-1)];
        slot.destination=clock_destination;
        slot.destination_size=clock_destination_size;
        slot.sequence=sequence;
        slot.status=status;
        clock_tail.store(tail+1,std::memory_order_release);
    }
    void receive(Board &board, OutputStage &output) {
        if(fd<0) return;
        uint8_t input[2048];
        while(true) {
            sockaddr_un sender{};
            socklen_t sender_size=sizeof(sender);
            auto count=recvfrom(fd,input,sizeof(input),0,reinterpret_cast<sockaddr*>(&sender),&sender_size);
            if(count>0) {
                // F0 7D 'U' path F7 subscribes the standalone panel clock receiver.
                // Registration is handled on this non-render engine thread.
                if(count>=6 && input[0]==0xf0 && input[1]==0x7d && input[2]=='U' && input[count-1]==0xf7) {
                    const size_t length=size_t(count)-4;
                    if(length && length<sizeof(clock_destination.sun_path) &&
                       std::find(input+3,input+3+length,uint8_t(0))==input+3+length) {
                        clock_destination={};
                        clock_destination.sun_family=AF_UNIX;
                        std::memcpy(clock_destination.sun_path,input+3,length);
                        clock_destination.sun_path[length]='\0';
                        clock_destination_size=static_cast<socklen_t>(offsetof(sockaddr_un,sun_path)+length+1);
                    }
                    continue;
                }
                // The board's own messages (src/core/panel.h) are applied there; the output
                // stage and the readback are this engine's.
                switch(apply_panel_message(board,input,size_t(count))) {
                case PanelMessage::applied:
                case PanelMessage::invalid:
                case PanelMessage::dropped:
                    continue;
                case PanelMessage::output:
                    // F0 7D 'G' gain F7 is the output trim after the original DSP's stereo
                    // converter stream. F0 7D 'X' which value F7: which 0 for the compressor
                    // knob, 1 for the reverb knob and 2 for the reverb's character (0 big room,
                    // 1 tight room).
                    if(input[2]=='G') output.set_output_gain(input[3]);
                    else if(input[3]<2 && input[4]<=127) output.fx.set_knob(input[3],input[4]);
                    else if(input[3]==2) output.fx.set_tight_room(input[4]!=0);
                    continue;
                case PanelMessage::readback: {
                    // Diagnostic readback for the desktop surface, returned only to the UNIX
                    // datagram peer that asked for it; it never enters the firmware UART.
                    // F0 7D 'Q' page F7: a 256-byte slice of controller RAM (page FE: the
                    // saturated execution counts from 1200). F0 7D 'C' hi lo F7: the counts for
                    // the 256 PCs from hi:lo.
                    std::array<uint8_t,261> reply{0xf0,0x7d,'R',input[3]};
                    if(input[2]=='C') {
                        size_t start=(size_t(input[3])<<8)|input[4];
                        for(unsigned i=0;i<256;++i)
                            reply[4+i]=uint8_t(std::min<uint64_t>(255,board.coverage[(start+i)&0xffff]));
                    } else if(input[3]==0xfe) {
                        for(unsigned i=0;i<256;++i) reply[4+i]=uint8_t(std::min<uint64_t>(255,board.coverage[0x1200+i]));
                    } else {
                        std::copy_n(board.ram.begin()+size_t(input[3])*256,256,reply.begin()+4);
                    }
                    reply[260]=0xf7;
                    sendto(fd,reply.data(),reply.size(),0,reinterpret_cast<sockaddr*>(&sender),sender_size);
                    continue;
                }
                case PanelMessage::midi:
                    break;
                }
                for(ssize_t i=0; i<count; ++i) {
                    if(is_transport_realtime(input[i])) forward_clock(input[i]);
                    else board.enqueue_midi(input[i]);
                }
                continue;
            }
            if(count<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) return;
            return;
        }
    }
};

// The 2 KiB EEPROM at B000..B7FF holds user programs 25..39 and the system
// settings. Realtime mode can keep it in a file so saves outlive the engine.
struct EepromFile {
    static constexpr size_t base=0xb000, size=0x800;
    std::filesystem::path path;
    std::vector<uint8_t> saved, pending;
    bool warned=false;
    static std::vector<uint8_t> image(const Board &board) {
        return {board.ram.begin()+base, board.ram.begin()+base+size};
    }
    bool load(Board &board) {
        if(path.empty() || !std::filesystem::exists(path)) return false;
        saved=read_rom(path,size);
        std::copy(saved.begin(),saved.end(),board.ram.begin()+base);
        return true;
    }
    // Replace the file in one rename so a crash cannot leave half an image.
    bool write(const Board &board) {
        auto current=image(board);
        auto temporary=path;
        temporary+=".tmp";
        std::ofstream f(temporary,std::ios::binary|std::ios::trunc);
        f.write(reinterpret_cast<const char*>(current.data()),current.size());
        f.close();
        std::error_code error;
        if(f) std::filesystem::rename(temporary,path,error);
        if(!f || error) return false;
        saved=std::move(current);
        return true;
    }
    // Fail at startup, where the panel reports it, not on the first save.
    void start(const Board &board) {
        if(path.empty()) return;
        pending=image(board);
        if(pending!=saved && !write(board)) throw std::runtime_error("Cannot write EEPROM file "+path.string());
    }
    // A program save is many byte writes, so wait for the image to hold
    // still between two calls before writing it out.
    void poll(const Board &board) {
        if(path.empty()) return;
        auto current=image(board);
        if(current!=pending) { pending=std::move(current); return; }
        if(pending!=saved && !write(board) && !warned) {
            warned=true;
            std::cerr << "Cannot write EEPROM file " << path.string() << "; saves will not persist.\n";
        }
    }
    void finish(const Board &board) {
        if(!path.empty() && image(board)!=saved) write(board);
    }
};

// Sequencer ports another client may read from, as (address, client name,
// port name). Names, not numbers, identify a device: numbers change when it
// is plugged in again.
struct MidiSource { snd_seq_addr_t address; std::string client, port; };
static std::vector<MidiSource> midi_sources(snd_seq_t *sequencer) {
    std::vector<MidiSource> sources;
    snd_seq_client_info_t *client; snd_seq_client_info_alloca(&client);
    snd_seq_port_info_t *port; snd_seq_port_info_alloca(&port);
    snd_seq_client_info_set_client(client,-1);
    while(snd_seq_query_next_client(sequencer,client)>=0) {
        int id=snd_seq_client_info_get_client(client);
        if(id==SND_SEQ_CLIENT_SYSTEM || id==snd_seq_client_id(sequencer)) continue;
        snd_seq_port_info_set_client(port,id);
        snd_seq_port_info_set_port(port,-1);
        while(snd_seq_query_next_port(sequencer,port)>=0) {
            unsigned caps=snd_seq_port_info_get_capability(port), readable=SND_SEQ_PORT_CAP_READ|SND_SEQ_PORT_CAP_SUBS_READ;
            if((caps&readable)!=readable || (caps&SND_SEQ_PORT_CAP_NO_EXPORT)) continue;
            sources.push_back({*snd_seq_port_info_get_addr(port),snd_seq_client_info_get_name(client),snd_seq_port_info_get_name(port)});
        }
    }
    return sources;
}

static int list_midi_sources() {
    snd_seq_t *sequencer=nullptr;
    int error=snd_seq_open(&sequencer,"default",SND_SEQ_OPEN_INPUT,0);
    if(error<0) throw std::runtime_error("Cannot open ALSA sequencer: "+std::string(snd_strerror(error)));
    auto close_sequencer=std::unique_ptr<snd_seq_t,decltype(&snd_seq_close)>(sequencer,snd_seq_close);
    for(const MidiSource &source : midi_sources(sequencer)) std::cout << source.client << '\t' << source.port << '\n';
    return 0;
}

static int realtime_main(int argc, char **argv) {
    std::filesystem::path romdir="roms", eeprom_path;
    std::string audio_device="default", midi_device="seq", midi_connect;
    std::string control_socket;
    unsigned rate=44100, period=128, channel=15;
    bool quiet=false, channel_given=false;
    for(int i=1; i<argc; ++i) {
        std::string opt=argv[i];
        if(opt=="--help") {
            std::cout << "usage: drumstation --realtime [--rom-dir DIR] [--audio DEVICE] [--midi seq|DEVICE|none]\n"
                         "                              [--midi-connect CLIENT:PORT] [--channel 1..16] [--rate HZ]\n"
                         "                              [--period FRAMES] [--control-socket PATH] [--eeprom FILE] [--quiet]\n"
                         "       drumstation --realtime --list-midi\n"
                         "Defaults: audio=default, midi=seq, channel=15, rate=44100, period=128.\n"
                         "--list-midi prints the sequencer ports that can send, as CLIENT<tab>PORT names.\n"
                         "--midi-connect cables one of them to the seq input, now or when it appears.\n"
                         "--eeprom keeps user programs and settings in FILE, created if missing.\n"
                         "A stored MIDI channel is kept unless --channel is given.\n";
            return 0;
        }
        if(opt=="--quiet") { quiet=true; continue; }
        if(opt=="--list-midi") return list_midi_sources();
        if((opt=="--rom-dir" || opt=="--audio" || opt=="--midi" || opt=="--midi-connect" || opt=="--channel" || opt=="--rate" || opt=="--period" || opt=="--control-socket" || opt=="--eeprom") && i+1<argc) {
            std::string value=argv[++i];
            if(opt=="--rom-dir") romdir=value;
            else if(opt=="--audio") audio_device=value;
            else if(opt=="--midi") midi_device=value;
            else if(opt=="--midi-connect") midi_connect=value;
            else if(opt=="--channel") { channel=std::stoul(value); channel_given=true; }
            else if(opt=="--rate") rate=std::stoul(value);
            else if(opt=="--period") period=std::stoul(value);
            else if(opt=="--eeprom") eeprom_path=value;
            else control_socket=value;
        } else throw std::runtime_error("Unknown/incomplete realtime option: "+opt);
    }
    if(channel<1 || channel>16 || rate<8000 || rate>192000 || period<16 || period>2048)
        throw std::runtime_error("Invalid realtime channel, rate, or period");
    Board board=load_board(romdir);
    board.experimental=true;
    board.sport_log=false;
    EepromFile eeprom{eeprom_path};
    if(!eeprom.load(board)) board.set_synthetic_eeprom(channel);
    else {
        // An image with an erased channel byte would leave MIDI ignored.
        for(unsigned address : {0xb7fb,0xb7fe})
            if(channel_given || board.ram[address]<1 || board.ram[address]>16) board.ram[address]=channel;
        channel=board.ram[0xb7fe];
    }
    eeprom.start(board);
    board.live_midi_byte_steps=uint64_t(rate)*756*10/31250;
    OutputStage output;
    output.fx.set_rate(rate);
    LocalMidiControl control;
    control.open(control_socket);
    snd_pcm_t *pcm=nullptr;
    snd_rawmidi_t *midi=nullptr;
    snd_seq_t *sequencer=nullptr;
    int sequencer_port=-1;
    int error=snd_pcm_open(&pcm,audio_device.c_str(),SND_PCM_STREAM_PLAYBACK,0);
    if(error<0) throw std::runtime_error("Cannot open audio device '"+audio_device+"': "+snd_strerror(error));
    auto close_pcm=std::unique_ptr<snd_pcm_t,decltype(&snd_pcm_close)>(pcm,snd_pcm_close);
    error=snd_pcm_set_params(pcm,SND_PCM_FORMAT_S16_LE,SND_PCM_ACCESS_RW_INTERLEAVED,2,rate,1,30000);
    if(error<0) throw std::runtime_error("Cannot configure audio device: "+std::string(snd_strerror(error)));
    if(midi_device=="seq") {
        error=snd_seq_open(&sequencer,"default",SND_SEQ_OPEN_INPUT,0);
        if(error<0) throw std::runtime_error("Cannot open ALSA sequencer input: "+std::string(snd_strerror(error)));
        snd_seq_set_client_name(sequencer,"DrumStation firmware emulator");
        sequencer_port=snd_seq_create_simple_port(sequencer,"DrumStation MIDI In",
            SND_SEQ_PORT_CAP_WRITE|SND_SEQ_PORT_CAP_SUBS_WRITE,
            SND_SEQ_PORT_TYPE_MIDI_GENERIC|SND_SEQ_PORT_TYPE_APPLICATION);
        if(sequencer_port<0) throw std::runtime_error("Cannot create ALSA sequencer port: "+std::string(snd_strerror(sequencer_port)));
    } else if(midi_device!="none") {
        error=snd_rawmidi_open(&midi,nullptr,midi_device.c_str(),SND_RAWMIDI_NONBLOCK);
        if(error<0) throw std::runtime_error("Cannot open MIDI input '"+midi_device+"': "+snd_strerror(error));
    }
    auto close_midi=std::unique_ptr<snd_rawmidi_t,decltype(&snd_rawmidi_close)>(midi,snd_rawmidi_close);
    auto close_sequencer=std::unique_ptr<snd_seq_t,decltype(&snd_seq_close)>(sequencer,snd_seq_close);
    std::signal(SIGINT,stop_realtime);
    std::signal(SIGTERM,stop_realtime);
    // A panel-launched engine must not outlive the panel: an orphaned
    // engine keeps the audio device and a CPU core busy.
    prctl(PR_SET_PDEATHSIG,SIGTERM);
    std::vector<int16_t> frames(period*2);
    uint64_t rendered_frames=0, eeprom_frames=0;
    uint64_t reported_midi=0;
    uint64_t reported_midi_drops=0, reported_batch_drops=0, reported_clock_drops=0;
    auto report_start=std::chrono::steady_clock::now();
    auto receive_midi_byte=[&](uint8_t value) {
        if(is_transport_realtime(value)) control.forward_clock(value);
        else board.enqueue_midi(value);
    };
    // The device asked for with --midi-connect, cabled once each time it
    // shows up: a cable someone then unplugs stays unplugged.
    auto connect_checked=report_start-std::chrono::seconds(1);
    int connected_client=-1, connected_port=-1;
    auto work_time=std::chrono::steady_clock::duration::zero();
    if(!quiet) std::cerr << "Realtime engine: audio=" << audio_device << " rate=" << rate << " MIDI=" << midi_device
                         << " channel=" << channel << ". Press Ctrl-C to stop.\n";
    if(sequencer && !quiet) std::cerr << "ALSA sequencer input: " << snd_seq_client_id(sequencer) << ':' << sequencer_port
                                      << " (DrumStation MIDI In)\n";
    while(realtime_running) {
        control.receive(board,output);
        if(sequencer && !midi_connect.empty() && std::chrono::steady_clock::now()-connect_checked>=std::chrono::seconds(1)) {
            connect_checked=std::chrono::steady_clock::now();
            int client=-1, port=-1;
            for(const MidiSource &source : midi_sources(sequencer))
                if(source.client+":"+source.port==midi_connect) { client=source.address.client; port=source.address.port; break; }
            if(client>=0 && (client!=connected_client || port!=connected_port)) {
                int result=snd_seq_connect_from(sequencer,sequencer_port,client,port);
                if(!quiet) std::cerr << "MIDI input " << midi_connect << (result<0 && result!=-EBUSY ? ": cannot connect\n" : ": connected\n");
            }
            connected_client=client; connected_port=port;
        }
        if(sequencer) {
            // Fetch from the kernel queue as well as checking the local queue.
            // A zero here only sees events already copied into libasound.
            while(snd_seq_event_input_pending(sequencer,1)>0) {
                snd_seq_event_t *event=nullptr;
                if(snd_seq_event_input(sequencer,&event)<0 || !event) break;
                switch(event->type) {
                case SND_SEQ_EVENT_CLOCK: receive_midi_byte(0xf8); break;
                case SND_SEQ_EVENT_START: receive_midi_byte(0xfa); break;
                case SND_SEQ_EVENT_CONTINUE: receive_midi_byte(0xfb); break;
                case SND_SEQ_EVENT_STOP: receive_midi_byte(0xfc); break;
                case SND_SEQ_EVENT_NOTEON:
                    receive_midi_byte((event->data.note.velocity ? 0x90 : 0x80)|event->data.note.channel);
                    receive_midi_byte(event->data.note.note);
                    receive_midi_byte(event->data.note.velocity);
                    break;
                case SND_SEQ_EVENT_NOTEOFF:
                    receive_midi_byte(0x80|event->data.note.channel);
                    receive_midi_byte(event->data.note.note);
                    receive_midi_byte(event->data.note.velocity);
                    break;
                case SND_SEQ_EVENT_CONTROLLER:
                    receive_midi_byte(0xb0|event->data.control.channel);
                    receive_midi_byte(event->data.control.param);
                    receive_midi_byte(event->data.control.value);
                    break;
                case SND_SEQ_EVENT_PGMCHANGE:
                    receive_midi_byte(0xc0|event->data.control.channel);
                    receive_midi_byte(event->data.control.value);
                    break;
                case SND_SEQ_EVENT_SYSEX:
                    for(unsigned i=0; i<event->data.ext.len; ++i)
                        receive_midi_byte(static_cast<const uint8_t*>(event->data.ext.ptr)[i]);
                    break;
                default: break;
                }
            }
        }
        if(midi) {
            unsigned char input[256];
            int count=snd_rawmidi_read(midi,input,sizeof(input));
            if(count>0) for(int i=0; i<count; ++i) receive_midi_byte(input[i]);
        }
        auto work_start=std::chrono::steady_clock::now();
        for(unsigned i=0; i<period; ++i) {
            auto frame=output.process(board.firmware_frame());
            frames[i*2]=frame[0]; frames[i*2+1]=frame[1];
        }
        work_time+=std::chrono::steady_clock::now()-work_start;
        snd_pcm_sframes_t written=snd_pcm_writei(pcm,frames.data(),period);
        if(written<0) { snd_pcm_prepare(pcm); continue; }
        while(written<static_cast<snd_pcm_sframes_t>(period)) {
            auto more=snd_pcm_writei(pcm,frames.data()+written*2,period-written);
            if(more<0) { snd_pcm_prepare(pcm); break; }
            written+=more;
        }
        rendered_frames+=period;
        if((eeprom_frames+=period)>=rate/4) { eeprom_frames=0; eeprom.poll(board); }
        auto elapsed=std::chrono::steady_clock::now()-report_start;
        if(!quiet && elapsed>=std::chrono::seconds(1)) {
            double wall=std::chrono::duration<double>(elapsed).count();
            double work=std::chrono::duration<double>(work_time).count();
            std::cerr << "Realtime: " << rendered_frames/wall << " frames/s, synthesis load " << (100.0*work/wall)
                      << "%, MIDI bytes " << (board.live_midi_delivered-reported_midi) << "\n";
            if(board.live_midi.refused!=reported_midi_drops ||
               board.sequencer_batches_dropped!=reported_batch_drops ||
               control.clock_dropped.load(std::memory_order_relaxed)!=reported_clock_drops) {
                std::cerr << "Realtime queue drops: MIDI=" << board.live_midi.refused
                          << " sequencer_batches=" << board.sequencer_batches_dropped
                          << " clock_forward=" << control.clock_dropped.load(std::memory_order_relaxed) << "\n";
            }
            report_start=std::chrono::steady_clock::now();
            rendered_frames=0;
            work_time=std::chrono::steady_clock::duration::zero();
            reported_midi=board.live_midi_delivered;
            reported_midi_drops=board.live_midi.refused;
            reported_batch_drops=board.sequencer_batches_dropped;
            reported_clock_drops=control.clock_dropped.load(std::memory_order_relaxed);
        }
    }
    eeprom.finish(board);
    snd_pcm_drain(pcm);
    return 0;
}

int main(int argc,char **argv) {
    try {
        if(argc>1 && std::string(argv[1])=="--realtime") return realtime_main(argc-1,argv+1);
        if(argc>1 && std::string(argv[1])=="--help") {
            std::cout << "usage: drumstation [ROMDIR OUTDIR DSP_STEPS] [--dsp-only] [--experimental-timers]\n"
                << "                   [--eeprom 2048-BYTE-FILE] [--midi RAW-BYTES | --midi-events TEXT]\n"
                << "                   [--no-sport-log] (still writes stereo.pcm) [--frames FILE]\n"
                << "                   [--live-events FILE]\n"
                << "Events: one decimal DSP step and hexadecimal UART byte per line, sorted by time.\n"
                << "--live-events FILE runs whole frames as the realtime engine does, queueing each\n"
                << "  line's bytes (FRAME then hexadecimal bytes) for the UART at that frame. A line\n"
                << "  FRAME panel BYTES is a panel message (F0 7D ... F7) for that frame instead,\n"
                << "  applied after the frame's UART bytes, as libgm_drumstation applies control\n"
                << "  events (anything but the board's own messages does nothing).\n"
                << "--frames writes the latest stereo pair after every 756 DSP steps, as S16LE before\n"
                << "the output boost (stereo.pcm has one frame per DSP word pair). Only with\n"
                << "--live-events does the board step as the realtime engine steps it, so only\n"
                << "then is it what the engine plays.\n";
            return 0;
        }
        std::filesystem::path romdir=argc>1?argv[1]:"roms", out=argc>2?argv[2]:"build/run";
        uint64_t cycles=argc>3?std::stoull(argv[3]):1000000;
        bool dsp_only=false, live=false;
        std::vector<std::pair<uint64_t,uint8_t>> live_events;
        std::vector<std::pair<uint64_t,std::vector<uint8_t>>> live_panel;
        std::ofstream frames;
        std::filesystem::create_directories(out);
        Board board=load_board(romdir);
        std::ofstream events(out/"events.tsv"), sport(out/"sport.tsv"), pcm(out/"stereo.pcm",std::ios::binary);
        if(!events || !sport || !pcm) throw std::runtime_error("Cannot initialize board/output");
        board.attach_traces(&events,&sport,&pcm);
        for(int i=4;i<argc;++i) {
            std::string opt=argv[i];
            if(opt=="--experimental-timers") board.experimental=true;
            else if(opt=="--no-sport-log") board.sport_log=false;
            else if(opt=="--dsp-only") dsp_only=true;
            else if(opt=="--frames" && i+1<argc) {
                frames.open(argv[++i],std::ios::binary|std::ios::trunc);
                if(!frames) throw std::runtime_error("Cannot write frames file");
            } else if(opt=="--eeprom" && i+1<argc) {
                auto image=read_rom(argv[++i],2048);
                std::copy(image.begin(),image.end(),board.ram.begin()+0xb000);
            } else if(opt=="--midi" && i+1<argc) {
                std::ifstream f(argv[++i],std::ios::binary);
                if(!f) throw std::runtime_error("Cannot open MIDI byte stream");
                board.midi.assign(std::istreambuf_iterator<char>(f),{});
            } else if(opt=="--midi-events" && i+1<argc) {
                std::ifstream f(argv[++i]);
                if(!f) throw std::runtime_error("Cannot open MIDI events");
                uint64_t tick; unsigned value;
                while(f>>std::dec>>tick) {
                    if(!(f>>std::hex>>value) || value>255 ||
                        (!board.midi_events.empty() && tick<board.midi_events.back().first))
                        throw std::runtime_error("Invalid/unsorted MIDI events");
                    board.midi_events.emplace_back(tick,value);
                }
                if(!f.eof()) throw std::runtime_error("Invalid MIDI event timestamp");
            } else if(opt=="--live-events" && i+1<argc) {
                std::ifstream f(argv[++i]);
                if(!f) throw std::runtime_error("Cannot open live MIDI events");
                std::string line;
                while(std::getline(f,line)) {
                    std::istringstream fields(line);
                    uint64_t frame; unsigned value;
                    if(!(fields>>std::dec>>frame)) continue;
                    if((!live_events.empty() && frame<live_events.back().first) ||
                       (!live_panel.empty() && frame<live_panel.back().first))
                        throw std::runtime_error("Unsorted live MIDI events");
                    const auto bytes_at=fields.tellg();
                    std::string word;
                    if(fields>>word && word=="panel") {
                        std::vector<uint8_t> message;
                        while(fields>>std::hex>>value) {
                            if(value>255) throw std::runtime_error("Invalid live panel byte");
                            message.push_back(uint8_t(value));
                        }
                        live_panel.emplace_back(frame,std::move(message));
                        continue;
                    }
                    fields.clear();
                    fields.seekg(bytes_at);
                    while(fields>>std::hex>>value) {
                        if(value>255) throw std::runtime_error("Invalid live MIDI byte");
                        live_events.emplace_back(frame,uint8_t(value));
                    }
                }
                live=true;
            } else throw std::runtime_error("Unknown/incomplete option: "+opt);
        }
        if(!board.midi.empty() && !board.midi_events.empty()) throw std::runtime_error("Choose one MIDI input format");
        if(live) {
            // As the realtime engine runs: whole frames, the controller on every eighth
            // step of each, and live bytes queued for the UART at their frame's start.
            if(!board.midi.empty() || !board.midi_events.empty() || dsp_only)
                throw std::runtime_error("--live-events runs alone, as the realtime engine");
            size_t next=0, next_panel=0;
            for(uint64_t frame=0; frame<cycles/756; ++frame) {
                for(; next<live_events.size() && live_events[next].first==frame; ++next)
                    board.enqueue_midi(live_events[next].second);
                for(; next_panel<live_panel.size() && live_panel[next_panel].first==frame; ++next_panel) {
                    // As the device takes a control event: anything but the board's own
                    // messages (output stage, readback, not a panel message) does nothing.
                    const auto &message=live_panel[next_panel].second;
                    apply_panel_message(board,message.data(),message.size());
                }
                auto pair=board.firmware_frame();
                if(frames.is_open())
                    for(uint16_t sample : {uint16_t(pair[0]),uint16_t(pair[1])}) {
                        frames.put(char(sample&255)); frames.put(char(sample>>8));
                    }
            }
            dump_board(board,out);
            return 0;
        }
        // Decided once: a stream check per step costs more than the step loop's own work.
        const bool write_frames=frames.is_open();
        for(uint64_t i=0;i<cycles;++i) {
            // Provisional 16 MHz DSP / (8 MHz MCU clock / 4) from MAME.
            if(!dsp_only && i%8==0) board.mcu.run(1);
            board.step();
            // The latest L/R pair after each 756 steps (this loop's own stepping, not the engine's).
            if(write_frames && (i+1)%756==0)
                for(uint16_t sample : {uint16_t(board.live_left),uint16_t(board.live_right)}) {
                    frames.put(char(sample&255)); frames.put(char(sample>>8));
                }
        }
        dump_board(board,out);
        return 0;
    } catch(const std::exception &e) { std::cerr << "drumstation: " << e.what() << '\n'; return 1; }
}
