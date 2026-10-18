// SPDX-License-Identifier: GPL-3.0-or-later
// Two boards in one process, stepped in alternation with different notes, must each
// produce exactly what that board produces running alone: nothing is shared between
// them. Links drumstation_core only (no ALSA). Exit 77 (skipped) without local ROMs.
#include "core/board.h"
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <memory>
#include <string>
#include <vector>

static std::vector<uint8_t> read_file(const std::string &path) {
    std::ifstream f(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(f), {}};
}

static constexpr unsigned rate=44100, frames=rate*3/2, note_at=rate/2;

static std::unique_ptr<Board> make_board(const std::vector<uint8_t> &u15, const std::vector<uint8_t> &u28) {
    auto board=std::make_unique<Board>(u15,u28);
    board->experimental=true;
    board->set_synthetic_eeprom(10);
    return board;
}

static void frame(Board &board, unsigned i, uint8_t note, std::vector<int16_t> &out) {
    if(i==note_at) for(uint8_t byte : {uint8_t(0x99),note,uint8_t(110)}) board.enqueue_midi(byte);
    auto f=board.firmware_frame();
    out.push_back(f[0]); out.push_back(f[1]);
}

int main() {
    const char *root=std::getenv("DRUMSTATION_ROOT");
    std::string roms=std::string(root ? root : ".")+"/roms/";
    auto u15=read_file(roms+"v1.2.u15"), u28=read_file(roms+"v1.2.u28");
    if(u15.size()!=32768 || u28.size()!=524288) { std::puts("skipped: no ROMs"); return 77; }

    std::vector<int16_t> solo_a, solo_b, a_out, b_out;
    {
        auto a=make_board(u15,u28);
        for(unsigned i=0;i<frames;++i) frame(*a,i,36,solo_a);
    }
    {
        auto b=make_board(u15,u28);
        for(unsigned i=0;i<frames;++i) frame(*b,i,38,solo_b);
    }
    auto a=make_board(u15,u28), b=make_board(u15,u28);
    for(unsigned i=0;i<frames;++i) { frame(*a,i,36,a_out); frame(*b,i,38,b_out); }

    int failures=0;
    auto peak=[](const std::vector<int16_t> &v) { int p=0; for(int16_t s : v) p=std::max(p,std::abs(int(s))); return p; };
    if(peak(solo_a)<100 || peak(solo_b)<100) { std::puts("FAIL: a solo board played nothing"); ++failures; }
    if(solo_a==solo_b) { std::puts("FAIL: notes 36 and 38 sounded the same"); ++failures; }
    if(a_out!=solo_a) { std::puts("FAIL: board A differs when B runs beside it"); ++failures; }
    if(b_out!=solo_b) { std::puts("FAIL: board B differs when A runs beside it"); ++failures; }
    if(!failures) std::printf("core_two_boards: both boards match their solo runs (%u frames, peaks %d / %d)\n",
                              frames,peak(solo_a),peak(solo_b));
    return failures ? 1 : 0;
}
