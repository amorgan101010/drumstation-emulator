// SPDX-License-Identifier: GPL-3.0-or-later
// Bounded DrumStation sequencer queues and MIDI transport-byte filtering.
#include "core/panel.h"
#include <array>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

static std::vector<uint8_t> read_file(const std::string &path) {
    std::ifstream f(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(f), {}};
}

int main() {
    const char *root=std::getenv("DRUMSTATION_ROOT");
    const std::string roms=std::string(root ? root : ".")+"/roms/";
    auto u15=read_file(roms+"v1.2.u15"), u28=read_file(roms+"v1.2.u28");
    if(u15.size()!=32768 || u28.size()!=524288) { std::puts("skipped: no ROMs"); return 77; }

    for(uint8_t status : {uint8_t(0xf8),uint8_t(0xfa),uint8_t(0xfb),uint8_t(0xfc)})
        if(!is_transport_realtime(status)) { std::puts("FAIL: supported realtime status not recognized"); return 1; }
    if(is_transport_realtime(0xfd) || is_transport_realtime(0x90)) {
        std::puts("FAIL: unrelated MIDI status misclassified as transport"); return 1;
    }

    Board midi_board(u15,u28);
    const std::array<uint8_t,9> interleaved{0x90,36,100,0xf8,0xfa,0xb0,7,64,0xfc};
    if(!enqueue_midi_message(midi_board,interleaved.data(),interleaved.size()) ||
       midi_board.live_midi.size()!=6) {
        std::puts("FAIL: clock transport bytes entered the firmware queue"); return 1;
    }
    const std::array<uint8_t,6> expected{0x90,36,100,0xb0,7,64};
    for(uint8_t byte : expected) {
        if(midi_board.live_midi.empty() || midi_board.live_midi.front()!=byte) {
            std::puts("FAIL: filtering changed ordinary MIDI byte order"); return 1;
        }
        midi_board.live_midi.pop_front();
    }
    for(size_t i=0;i<decltype(midi_board.live_midi)::capacity-2;++i)
        midi_board.enqueue_midi(0x01);
    const auto midi_size=midi_board.live_midi.size();
    const std::array<uint8_t,3> note{0x90,38,90};
    if(enqueue_midi_message(midi_board,note.data(),note.size()) ||
       midi_board.live_midi.size()!=midi_size || midi_board.midi_messages_dropped!=1) {
        std::puts("FAIL: a saturated firmware queue partially accepted a MIDI event"); return 1;
    }

    Board batch_board(u15,u28);
    for(size_t i=0;i<decltype(batch_board.sequencer_ram_writes)::capacity-1;++i)
        batch_board.schedule_ram_write(0,0x0101,64,0x7f);
    const std::array<uint8_t,15> batch{
        0xf0,0x7d,'T',2,
        0x21,0x01,0x01,40,64,0x7f,
        0x30,0,36,100,
        0xf7,
    };
    const auto writes_before=batch_board.sequencer_ram_writes.size();
    const auto midi_before=batch_board.live_midi.size();
    if(apply_panel_message(batch_board,batch.data(),batch.size())!=PanelMessage::dropped ||
       batch_board.sequencer_ram_writes.size()!=writes_before ||
       batch_board.live_midi.size()!=midi_before || batch_board.sequencer_batches_dropped!=1) {
        std::puts("FAIL: a saturated lock queue partially accepted a sequencer batch"); return 1;
    }
    std::puts("panel_transport_test: transport filtering and all-or-nothing overflow passed");
    return 0;
}
