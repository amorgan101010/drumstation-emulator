// SPDX-License-Identifier: GPL-3.0-or-later
// drumstation_core: the DrumStation board (MN1880 controller, ADSP-2181 DSP, their
// bus, timers and UART), with no host I/O: ROMs
// come in as bytes, traces go to streams the host attaches, and nothing here opens
// a file, a device or a socket. Several boards may run in one process.
#ifndef DRUMSTATION_CORE_BOARD_H
#define DRUMSTATION_CORE_BOARD_H
#include "adsp2181_core.h"
#include "mn1880.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <memory>
#include <mutex>
#include <ostream>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

// A fixed-capacity FIFO for the step loop: no allocation or release while it
// runs. A push past capacity is refused and counted (a deque would have grown).
template<typename T, size_t Capacity> struct Ring {
    static_assert((Capacity & (Capacity-1))==0, "Capacity must be a power of two");
    static constexpr size_t capacity=Capacity;
    std::array<T,Capacity> items{};
    size_t head=0, tail=0;  // tail-head entries, at items[head..tail) mod Capacity
    uint64_t refused=0;
    bool empty() const { return head==tail; }
    size_t size() const { return tail-head; }
    bool push_back(const T &item) {
        if(size()==Capacity) { ++refused; return false; }
        items[tail++ & (Capacity-1)]=item;
        return true;
    }
    const T &front() const { return items[head & (Capacity-1)]; }
    void pop_front() { ++head; }
};

struct Board {
    // The core builds shared decode tables on its first create, unguarded:
    // build them once, under call_once, before any board's own create.
    static adsp2181_t *create_dsp() {
        static std::once_flag tables;
        std::call_once(tables,[] { adsp2181_destroy(adsp2181_create()); });
        return adsp2181_create();
    }
    std::unique_ptr<adsp2181_t, decltype(&adsp2181_destroy)> dsp{create_dsp(), adsp2181_destroy};
    mn1880_device mcu;
    std::vector<uint8_t> program, bytes;
    std::array<uint8_t, 65536> ram{};
    std::array<uint64_t, 65536> reads{}, writes{}, coverage{};
    uint8_t latch=0, high=0;
    uint64_t transfers=0, idma_writes=0, dsp_steps=0;
    // Diagnostic traces, owned by the host (attach_traces); null when not wanted.
    std::ostream *events=nullptr;
    bool timer_enabled=false, experimental=false;
    uint64_t timer_phase=0, timer_irqs=0, tx_words=0;
    std::ostream *sport=nullptr;
    std::vector<uint8_t> midi;
    size_t midi_pos=0;
    uint64_t next_midi=2000000;
    std::vector<std::pair<uint64_t,uint8_t>> midi_events;
    size_t event_pos=0;
    // UART bytes waiting for the firmware; 64 KiB is about 20 s of DIN-rate input.
    Ring<uint8_t,1u<<16> live_midi;
    uint64_t live_midi_delivered=0;
    uint64_t sequencer_batches_dropped=0, sequencer_events_dropped=0, midi_messages_dropped=0;
    // DSP steps per 10-bit MIDI byte at 44.1 kHz (756 steps per frame).
    uint64_t live_midi_byte_steps=44100ull*756*10/31250, next_live_midi=0;
    struct ScheduledRamWrite { uint64_t at; unsigned address; uint8_t value, mask; };
    Ring<ScheduledRamWrite,1u<<12> sequencer_ram_writes;
    uint64_t sequencer_tail_steps=0;
    std::ostream *pcm=nullptr;
    bool sport_log=true, have_left=false;
    int16_t left_sample=0;
    int16_t live_left=0, live_right=0;
    // Physical pot positions are kept separate. E0..F5 are live controller
    // work RAM, so they must never be overlaid with these ADC values.
    // The peripheral conversion/scan itself is not mapped yet.
    std::array<uint8_t,22> panel_analog{};
    // The keypad and switches are separate active-low digital inputs for the
    // same reason: controller scratch writes must not release a held key.
    std::array<uint8_t,0x20> panel_buttons{};
    // The keypad is read on port 09 bits 2..5 while the firmware scans four
    // rows by writing E0/D0/B0/70 to the 9800 output latch.
    std::array<uint8_t,4> panel_matrix{};
    // Port 08 bits 7..5 for switch-scan columns FE/FD/FB/F7 (U15 4e8f).
    std::array<uint8_t,4> panel_switches{};
    bool record=false;

    // The ROM images as bytes: U15, the controller program (32 KiB), and U28,
    // the DSP code and samples (512 KiB). The board opens no files.
    Board(std::vector<uint8_t> u15, std::vector<uint8_t> u28)
        : program(std::move(u15)), bytes(std::move(u28)) {
        if (program.size()!=32768 || bytes.size()!=524288) throw std::runtime_error("Wrong ROM size");
        if (!dsp) throw std::runtime_error("Cannot initialize board");
        adsp2181_set_user(dsp.get(),this);
        adsp2181_set_callbacks(dsp.get(),nullptr,
            [](adsp2181_t *cpu,int port,int32_t value) {
                Board *active=static_cast<Board*>(adsp2181_user(cpu));
                ++active->tx_words;
                unsigned phase=adsp2181_dm(active->dsp.get())[0x3df0];
                if(active->record && active->sport_log && active->sport) *active->sport << active->dsp_steps << '\t' << port << '\t' << int16_t(value)
                    << '\t' << phase << '\n';
                // TX1 phase 2 is left and phase 4 is right: firmware Pan L4
                // silences phase 4, and the panned factory demo's stereo
                // balance matches the hardware recording only this way round.
                if(port==1 && phase==2) { active->left_sample=int16_t(value); active->have_left=true; }
                else if(port==1 && phase==4 && active->have_left) {
                    if(active->pcm) for(uint16_t sample : {uint16_t(active->left_sample),uint16_t(value)}) {
                        active->pcm->put(char(sample&255)); active->pcm->put(char(sample>>8));
                    }
                    active->have_left=false;
                    active->live_left=active->left_sample;
                    active->live_right=int16_t(value);
                }
            },
            [](adsp2181_t *cpu,int enabled) {
                Board *active=static_cast<Board*>(adsp2181_user(cpu));
                active->timer_enabled=enabled; active->timer_phase=0;
            });
        // Erased EEPROM placeholder, not a recovered factory/user EEPROM image.
        std::fill(ram.begin()+0xb000, ram.begin()+0xb800, 0xff);
        panel_buttons.fill(0xff);
        panel_matrix.fill(0x0f);
        // Column FE bit 7 is the WRITE switch, copied to RAM 8C bit 6.
        // Low is Protect, the manual's normal position; high is Enable,
        // where keypad digits pick a save slot instead of recalling it.
        panel_switches.fill(0xe0);
        panel_switches[0]&=0x7f;
        // Stationary synthetic ADC midpoint. A zero-filled ADC makes the
        // firmware see its master volume knob at zero and mutes CC7 changes.
        ram[0x1c]=0x80;
        panel_analog.fill(0x80);
        auto pm=adsp2181_pm(dsp.get());
        for (unsigned i=0; i<32; ++i) pm[i]=word24(i*3);
        mcu.m_cache.read_byte=[this](uint16_t a) { return a<program.size() ? program[a] : uint8_t(0xff); };
        mcu.m_data.read_byte=[this](uint16_t a) {
            ++reads[a];
            if (a==0x12) return mcu.ie0_r();
            if (a==0x15) return mcu.ie1_r();
            if (a==0x16) return mcu.cpum_r();
            if (a==0x09) {
                unsigned row=4;
                switch (ram[0x9800]&0xf0) {
                    case 0xe0: row=0; break;
                    case 0xd0: row=1; break;
                    case 0xb0: row=2; break;
                    case 0x70: row=3; break;
                }
                return uint8_t((panel_buttons[9]&0xc3) | ((row<4 ? panel_matrix[row] : 0x0f)<<2));
            }
            // Only the low physical input ports are external. Addresses such
            // as 19, 1A and 1B are firmware state and UART work registers.
            if (a==0x08) {
                unsigned column=4;
                switch (ram[0x9800]&0x0f) {
                    case 0x0e: column=0; break;
                    case 0x0d: column=1; break;
                    case 0x0b: column=2; break;
                    case 0x07: column=3; break;
                }
                if (column<4) return uint8_t((panel_buttons[8]&0x1f) | panel_switches[column]);
            }
            if (a>=0x02 && a<=0x0e) return panel_buttons[a];
            return ram[a];
        };
        mcu.m_data.write_byte=[this](uint16_t a, uint8_t v) {
            ++writes[a];
            if (record && writes[a]<=16 && (a<0x60 || a>=0x8000))
                *events << "mcu-write\t" << std::hex << mcu.get_active_cpu().irp << '\t' << a << '\t' << unsigned(v) << '\n';
            ram[a]=v;
            if (a==0x12) mcu.ie0_w(v);
            else if (a==0x15) mcu.ie1_w(v);
            else if (a==0x16) mcu.cpum_w(v);
            else if (a==0x8000) high=v;
            else if (a==0x9000) latch=v;
            else if (a==0x8800) {
                uint16_t data=(uint16_t(high)<<8)|v;
                if ((latch&0xc0)==0xc0) adsp2181_idma_addr_write(dsp.get(),data);
                else if ((latch&0xc0)==0) { adsp2181_idma_data_write(dsp.get(),data); ++idma_writes; }
            }
        };
        mcu.trace=[this](uint16_t pc) { ++coverage[pc]; };
    }
    // The offline runner's traces: event log, SPORT words, and the stereo PCM as
    // each word pair arrives. Without them the board records nothing.
    void attach_traces(std::ostream *event_log,std::ostream *sport_words,std::ostream *word_pcm) {
        events=event_log; sport=sport_words; pcm=word_pcm; record=event_log!=nullptr;
    }
    uint32_t word24(size_t pos) const {
        return (uint32_t(byte(pos))<<16)|(uint32_t(byte(pos+1))<<8)|byte(pos+2);
    }
    uint8_t byte(size_t pos) const { return bytes[pos & (bytes.size()-1)]; }
    void set_synthetic_eeprom(unsigned channel) {
        if (channel < 1 || channel > 16) throw std::runtime_error("MIDI channel must be 1..16");
        ram[0xb7fb]=channel; // MIDI TX channel
        ram[0xb7fc]=12;      // demo tempo
        ram[0xb7fd]=0;       // factory program 00
        ram[0xb7fe]=channel; // MIDI RX channel
    }
    bool enqueue_midi(uint8_t value) { return live_midi.push_back(value); }
    bool schedule_ram_write(uint64_t at,unsigned address,uint8_t value,uint8_t mask) {
        return sequencer_ram_writes.push_back({at,address,value,mask});
    }
    // Spans, so a caller can parse into fixed arrays: nothing here allocates.
    bool sequence_batch(std::span<const uint8_t> controls,std::span<const uint8_t> notes,
                        std::span<const uint8_t> resets,
                        std::span<const std::array<uint8_t,4>> sets,
                        std::span<const std::array<uint8_t,5>> locks) {
        const size_t midi_count=controls.size()+notes.size()+resets.size();
        const size_t write_count=sets.size()+2*locks.size();
        if(midi_count>live_midi.capacity-live_midi.size() ||
           write_count>sequencer_ram_writes.capacity-sequencer_ram_writes.size()) {
            ++sequencer_batches_dropped;
            sequencer_events_dropped+=midi_count+write_count;
            return false;
        }
        const uint64_t first_byte=std::max(dsp_steps,next_live_midi)+live_midi.size()*live_midi_byte_steps;
        const uint64_t start=std::max(first_byte,sequencer_tail_steps);
        const uint64_t writes_at=start+controls.size()*live_midi_byte_steps;
        for(const auto &write:sets) {
            unsigned address=(unsigned(write[0])<<8)|write[1];
            schedule_ram_write(writes_at,address,write[2],write[3]);
        }
        for(const auto &lock:locks) {
            unsigned address=(unsigned(lock[0])<<8)|lock[1];
            schedule_ram_write(writes_at,address,lock[2],lock[4]);
        }
        const uint64_t restore_at=start+(controls.size()+notes.size())*live_midi_byte_steps+
                                  live_midi_byte_steps;
        for(const auto &lock:locks) {
            unsigned address=(unsigned(lock[0])<<8)|lock[1];
            schedule_ram_write(restore_at,address,lock[3],lock[4]);
        }
        for(uint8_t byte:controls) enqueue_midi(byte);
        for(uint8_t byte:notes) enqueue_midi(byte);
        for(uint8_t byte:resets) enqueue_midi(byte);
        const uint64_t midi_end=start+(controls.size()+notes.size()+resets.size())*live_midi_byte_steps+
                                live_midi_byte_steps;
        sequencer_tail_steps=std::max(restore_at,midi_end);
        return true;
    }
    void set_panel_analog(unsigned index,uint8_t value) {
        if(index>=22) throw std::runtime_error("Panel analog index out of range");
        panel_analog[index]=value;
    }
    void set_panel_button(unsigned port,unsigned bit,bool pressed) {
        if(port>=0x20 || bit>=8) throw std::runtime_error("Panel button port/bit out of range");
        if(pressed) panel_buttons[port]&=~(1u<<bit);
        else panel_buttons[port]|=1u<<bit;
    }
    void set_panel_key(unsigned row,unsigned column,bool pressed) {
        if(row>=4 || column>=4) throw std::runtime_error("Panel key row/column out of range");
        if(pressed) panel_matrix[row]&=~(1u<<column);
        else panel_matrix[row]|=1u<<column;
    }
    void set_panel_switch(unsigned column,unsigned bit,bool high) {
        if(column>=4 || bit<5 || bit>=8) throw std::runtime_error("Panel switch column/bit out of range");
        if(high) panel_switches[column]|=1u<<bit;
        else panel_switches[column]&=~(1u<<bit);
    }
    void bdma() {
        auto dm=adsp2181_dm(dsp.get());
        unsigned count=dm[0x3fe4]&0x3fff;
        if (!count) return;
        unsigned control=dm[0x3fe3], type=control&3, dest=dm[0x3fe1]&0x3fff;
        size_t source=((control>>8)<<14)|(dm[0x3fe2]&0x3fff);
        unsigned stride=type==0?3:type==1?2:1;
        if (control&4) throw std::runtime_error("BDMA store to ROM requested");
        if (control&0xf0) throw std::runtime_error("Unimplemented BDMA control bits");
        // The 512 KiB ROM exposes 19 address bits; interpolation can fetch
        // across its physical end (observed source 0x7fffe, two DM words).
        if (dest+count>0x4000)
            throw std::runtime_error("BDMA range exceeds ROM or internal memory: source="+std::to_string(source)+
                " dest="+std::to_string(dest)+" count="+std::to_string(count)+" type="+std::to_string(type));
        if(record && transfers<256) *events << "bdma\t" << std::hex << adsp2181_pc(dsp.get()) << '\t' << source << '\t' << dest << '\t' << count << '\t' << type << '\n';
        auto pm=adsp2181_pm(dsp.get());
        for (unsigned i=0; i<count; ++i) {
            if (type==0) pm[dest+i]=word24(source);
            else if (type==1) dm[dest+i]=(uint16_t(byte(source))<<8)|byte(source+1);
            else dm[dest+i]=uint16_t(byte(source))<<(type==2?8:0);
            source+=stride;
        }
        dm[0x3fe1]=(dest+count)&0x3fff;
        dm[0x3fe2]=source&0x3fff;
        dm[0x3fe3]=(control&0xff)|((source>>14)<<8);
        dm[0x3fe4]=0;
        ++transfers;
        // Functional, instantaneous DMA. Timing/IRQ completion still unmodeled.
        if (control&8) { adsp2181_reset(dsp.get()); }
    }
    void step() {
        adsp2181_run(dsp.get(),1);
        ++dsp_steps;
        while(!sequencer_ram_writes.empty() && sequencer_ram_writes.front().at<=dsp_steps) {
            const auto write=sequencer_ram_writes.front();
            sequencer_ram_writes.pop_front();
            ram[write.address]=(ram[write.address]&~write.mask)|(write.value&write.mask);
        }
        bdma();
        if(timer_enabled) {
            auto dm=adsp2181_dm(dsp.get());
            if(++timer_phase >= uint64_t((dm[0x3ffb]&255)+1)) {
                timer_phase=0;
                if(dm[0x3ffc]==0) {
                    dm[0x3ffc]=dm[0x3ffd];
                    adsp2181_set_irq(dsp.get(),ADSP2181_TIMER,1);
                    adsp2181_set_irq(dsp.get(),ADSP2181_TIMER,0);
                    ++timer_irqs;
                } else --dm[0x3ffc];
            }
        }
        // Diagnostic hypothesis: MCU IRQ6 is the periodic service interrupt.
        // The handler at 59ec increments the counter polled at 2f60.
        // Its actual prescaler/period are not established yet.
        if(experimental && dsp_steps%16000==0) mcu.m_if |= 1<<6;
        // Live bytes arrive at the DIN rate. The firmware's receive ring
        // (handler 3252) holds 32 bytes; delivering a socket or sequencer
        // burst faster than 31250 baud overflows it and drops whole
        // messages, which desyncs running status.
        if(!live_midi.empty() && dsp_steps>=next_live_midi && !(mcu.m_if&(1<<9)) && (mcu.m_cpu[0].ie&(1<<9))) {
            ram[0x1b]=live_midi.front();
            live_midi.pop_front();
            next_live_midi=dsp_steps+live_midi_byte_steps;
            mcu.m_if |= 1<<9;
            ++live_midi_delivered;
            if(record) *events << "midi-rx\t" << std::hex << unsigned(ram[0x1b]) << '\n';
        }
        if(event_pos<midi_events.size() && dsp_steps>=midi_events[event_pos].first &&
            !(mcu.m_if&(1<<9)) && (mcu.m_cpu[0].ie&(1<<9))) {
            ram[0x1b]=midi_events[event_pos++].second;
            mcu.m_if |= 1<<9;
            if(record) *events << "midi-rx\t" << std::hex << unsigned(ram[0x1b]) << '\n';
        }
        // UART receive register/IRQ9 inferred from handler at U15:3252.
        // Space bytes by 320 us at the provisional 16 MHz DSP clock.
        if(dsp_steps>=next_midi && midi_pos<midi.size() && !(mcu.m_if&(1<<9)) &&
            (mcu.m_cpu[0].ie&(1<<9))) {
            ram[0x1b]=midi[midi_pos++];
            mcu.m_if |= 1<<9;
            next_midi=dsp_steps+5120;
            if(record) *events << "midi-rx\t" << std::hex << unsigned(ram[0x1b]) << '\n';
        }
    }
    // One output frame: 756 DSP steps (four phases of the firmware's 189-cycle
    // timer) with the controller interleaved, and the stereo pair the DSP sent
    // last, as the firmware made it (before the emulator's OutputStage).
    std::array<int16_t,2> firmware_frame() {
        for(unsigned i=0; i<756; ++i) {
            if(i%8==0) mcu.run(1);
            step();
        }
        return {live_left, live_right};
    }
};

#endif
