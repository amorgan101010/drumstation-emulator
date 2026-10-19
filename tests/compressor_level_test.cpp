// SPDX-License-Identifier: GPL-3.0-or-later
#include "core/fx.h"
#include <cmath>
#include <cstdio>

static double level(unsigned compressor) {
    OutputStage output;
    output.set_output_gain(64);
    output.fx.set_knob(0, compressor);
    double power=0;
    constexpr int rate=44100;
    for(int i=0; i<rate*8; ++i) {
        int phase=i%(rate/4);
        double envelope=std::exp(-phase/2500.0);
        double input=14000*envelope*(0.7*std::sin(i*0.08)+0.3*std::sin(i*0.22));
        auto sample=output.process({int16_t(input),int16_t(input)});
        if(i>=rate*2) power+=double(sample[0])*sample[0];
    }
    return std::sqrt(power/(rate*6));
}

int main() {
    double dry=level(0), wet=level(127);
    double ratio=wet/dry;
    std::printf("COMP 127 / dry RMS: %.3f\n",ratio);
    return ratio>=0.9 && ratio<=1.15 ? 0 : 1;
}
