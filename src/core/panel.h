// SPDX-License-Identifier: GPL-3.0-or-later
// The desktop panel's control messages, F0 7D <op> ... F7, applied to a board. One parser for
// the standalone engine's control socket and for libgm_drumstation's control events, so the
// panel does the same thing to the board in both. Nothing here allocates or throws: the device
// applies these inside render.
//
// A message is the board's (applied here), the output stage's ('G', 'X') or a readback ('Q',
// 'C'), both of which are the host's, or not a panel message at all: anything else, a malformed
// one included, is raw MIDI for the UART, as the control socket has always taken it.
#ifndef DRUMSTATION_CORE_PANEL_H
#define DRUMSTATION_CORE_PANEL_H
#include "board.h"
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

enum class PanelMessage { applied, invalid, dropped, output, readback, midi };

// These transport messages are consumed by the host clock router. They never
// enter the emulated UART, even when an input stream interleaves them with MIDI.
inline constexpr bool is_transport_realtime(uint8_t byte) {
    return byte==0xf8 || byte==0xfa || byte==0xfb || byte==0xfc;
}

// The embedded ABI can be called by a host that does not use gm-engine's graph
// interceptor. Suppress transport bytes there too and refuse an entire event if
// its bounded firmware queue cannot accept the remaining bytes.
inline bool enqueue_midi_message(Board &board, const uint8_t *data, size_t size) {
    size_t count=0;
    for(size_t i=0;i<size;++i) if(!is_transport_realtime(data[i])) ++count;
    if(count>board.live_midi.capacity-board.live_midi.size()) {
        ++board.midi_messages_dropped;
        return false;
    }
    for(size_t i=0;i<size;++i) if(!is_transport_realtime(data[i])) board.enqueue_midi(data[i]);
    return true;
}

// The per-voice LEVEL bytes of the recalled program image: the real pots emit no MIDI
// controller, so the panel edits these bytes and no others ('L', and a 'T' batch's writes to
// them must be whole 7-bit values).
inline constexpr std::array<unsigned,27> panel_level_addresses{
    0x101,0x107,0x10b,0x111,0x115,0x11a,0x11f,0x124,0x129,
    0x12e,0x133,0x138,0x13e,0x142,0x146,0x14c,0x151,0x156,
    0x15b,0x15f,0x165,0x16a,0x16e,0x172,0x176,0x17a,0x17e
};

inline bool panel_level_address(unsigned address) {
    return std::find(panel_level_addresses.begin(),panel_level_addresses.end(),address)!=panel_level_addresses.end();
}

// The program image at 0100..0187 and the unpacked GM-kit byte 018B.
inline bool panel_program_address(unsigned address) {
    return (address>=0x100 && address<0x188) || address==0x18b;
}

// Ordered sequencer batch: F0 7D 'T' count ops... F7. MIDI controller changes and notes enter
// the normal rate-limited firmware UART; the panel-only program writes are clocked around that
// same byte stream and restored after the firmware has had time to accept the note-on.
inline PanelMessage apply_sequence_batch(Board &board, const uint8_t *input, size_t count) {
    // At most 255 ops, each adding at most one entry (three bytes of MIDI) to one list.
    std::array<uint8_t,255*3> controls, notes, resets;
    std::array<std::array<uint8_t,4>,255> sets;
    std::array<std::array<uint8_t,5>,255> locks;
    size_t ncontrols=0, nnotes=0, nresets=0, nsets=0, nlocks=0;
    size_t pos=4;
    bool valid=input[count-1]==0xf7;
    for(unsigned op=0; valid && op<input[3]; ++op) {
        if(pos>=count-1) { valid=false; break; }
        uint8_t type=input[pos++];
        if(type==0x10 || type==0x11) {
            size_t size=type==0x10 ? 3 : 4;
            if(pos+size>count-1) { valid=false; break; }
            uint8_t channel=input[pos++], cc=input[pos++], value=input[pos++];
            uint8_t base=type==0x11 ? input[pos++] : value;
            if(channel>15 || cc>119 || value>127 || base>127) { valid=false; break; }
            for(uint8_t b : {uint8_t(0xb0|channel),cc,value}) controls[ncontrols++]=b;
            if(type==0x11) for(uint8_t b : {uint8_t(0xb0|channel),cc,base}) resets[nresets++]=b;
        } else if(type==0x20 || type==0x21) {
            size_t size=type==0x20 ? 4 : 5;
            if(pos+size>count-1) { valid=false; break; }
            uint8_t hi=input[pos++], lo=input[pos++], value=input[pos++];
            uint8_t base=type==0x21 ? input[pos++] : value;
            uint8_t mask=input[pos++];
            unsigned address=(unsigned(hi)<<8)|lo;
            if(!panel_program_address(address) || !mask ||
               (panel_level_address(address) && (mask!=0x7f || value>127 || base>127))) { valid=false; break; }
            if(type==0x20) sets[nsets++]={hi,lo,value,mask};
            else locks[nlocks++]={hi,lo,value,base,mask};
        } else if(type==0x30) {
            if(pos+3>count-1) { valid=false; break; }
            uint8_t channel=input[pos++], note=input[pos++], velocity=input[pos++];
            if(channel>15 || note>127 || velocity>127) { valid=false; break; }
            for(uint8_t b : {uint8_t((velocity ? 0x90 : 0x80)|channel),note,velocity}) notes[nnotes++]=b;
        } else { valid=false; break; }
    }
    valid=valid && pos==count-1;
    if(!valid) return PanelMessage::invalid;
    if(!board.sequence_batch({controls.data(),ncontrols},{notes.data(),nnotes},{resets.data(),nresets},
                             {sets.data(),nsets},{locks.data(),nlocks})) return PanelMessage::dropped;
    return PanelMessage::applied;
}

inline PanelMessage apply_panel_message(Board &board, const uint8_t *input, size_t count) {
    if(count<5 || input[0]!=0xf0 || input[1]!=0x7d) return PanelMessage::midi;
    const uint8_t op=input[2];
    if(op=='T') return apply_sequence_batch(board,input,count);
    auto shaped=[&](size_t size) { return count==size && input[size-1]==0xf7; };
    // F0 7D 'G' gain F7, and F0 7D 'X' which value F7: the output trim and the master effects,
    // after the DSP's converter stream. They never reach the firmware.
    if((op=='G' && shaped(5)) || (op=='X' && shaped(6))) return PanelMessage::output;
    // F0 7D 'Q' page F7 and F0 7D 'C' hi lo F7: controller RAM and coverage readback.
    if((op=='Q' && shaped(5)) || (op=='C' && shaped(6))) return PanelMessage::readback;
    // Panel analog latch: F0 7D 'A' channel value F7. The ADC conversion/scan still needs a
    // verified peripheral model.
    if(op=='A' && shaped(6)) {
        if(input[3]>=board.panel_analog.size()) return PanelMessage::invalid;
        board.set_panel_analog(input[3],input[4]);
        return PanelMessage::applied;
    }
    // A voice LEVEL: F0 7D 'L' hi lo value F7, its byte in the recalled program image. Until the
    // pots' ADC peripheral is mapped, the level addresses alone, no general RAM writes.
    if(op=='L' && shaped(7)) {
        unsigned address=(unsigned(input[3])<<8)|input[4];
        if(input[5]>127 || !panel_level_address(address)) return PanelMessage::invalid;
        board.ram[address]=(board.ram[address]&0x80)|input[5];
        return PanelMessage::applied;
    }
    // Utility-mode program fields (velocity flags, note-off, output set, distortion) have no MIDI
    // controller: the masked bits of one program byte, F0 7D 'P' hi lo value mask F7.
    if(op=='P' && shaped(8)) {
        unsigned address=(unsigned(input[3])<<8)|input[4];
        if(!panel_program_address(address)) return PanelMessage::invalid;
        board.ram[address]=(board.ram[address]&~input[6])|(input[5]&input[6]);
        return PanelMessage::applied;
    }
    if(shaped(7)) {
        // Active-low digital panel line: F0 7D 'B' port bit down F7.
        if(op=='B') {
            if(input[3]>=board.panel_buttons.size() || input[4]>=8) return PanelMessage::invalid;
            board.set_panel_button(input[3],input[4],input[5]!=0);
            return PanelMessage::applied;
        }
        // Scanned panel switch line: F0 7D 'S' column bit high F7.
        if(op=='S') {
            if(input[3]>=board.panel_switches.size() || input[4]<5 || input[4]>=8) return PanelMessage::invalid;
            board.set_panel_switch(input[3],input[4],input[5]!=0);
            return PanelMessage::applied;
        }
        // Firmware keypad matrix position: F0 7D 'K' row column pressed F7.
        if(op=='K') {
            if(input[3]>=board.panel_matrix.size() || input[4]>=4) return PanelMessage::invalid;
            board.set_panel_key(input[3],input[4],input[5]!=0);
            return PanelMessage::applied;
        }
    }
    return PanelMessage::midi;
}

#endif
