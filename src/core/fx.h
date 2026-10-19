// SPDX-License-Identifier: GPL-3.0-or-later
// The emulator's own output stage, after the DrumStation firmware's converter
// stream: the output boost and the master effects. Not part of the instrument and
// not part of drumstation_core's board: the standalone engine runs it on the
// board's frames, and the studio can run it as an effect of its own.
#ifndef DRUMSTATION_CORE_FX_H
#define DRUMSTATION_CORE_FX_H
#include <algorithm>
#include <array>
#include <cmath>
#include <numbers>
#include <cstdint>
#include <vector>

// Master effects on the stereo output, after the firmware's converter stream
// and the output boost: a reverb and a compressor with one knob each. They are
// the emulator's own, not part of the instrument. With both knobs at zero the
// board bypasses them, so firmware renders stay bit-exact.
struct MasterFx {
    // Delay lengths in frames at 44.1 kHz and the right channel's offset are
    // the tuning of Jezar's public-domain Freeverb.
    static constexpr std::array<unsigned,8> comb_frames{1116,1188,1277,1356,1422,1491,1557,1617};
    static constexpr std::array<unsigned,4> allpass_frames{556,441,341,225};
    static constexpr unsigned spread_frames=23;
    struct Line {
        std::vector<float> data;
        size_t pos=0;
        float store=0;
        float &head() { return data[pos]; }
        void advance() { if(++pos==data.size()) pos=0; }
    };
    std::array<std::array<Line,8>,2> combs;
    std::array<std::array<Line,4>,2> allpasses;
    uint8_t knobs[2]{}; // compressor, reverb: 0..127
    bool tight_room=false; // which of the reverb's two characters
    float compress=0, reverb=0; // the knobs as 0..1, smoothed against zipper noise
    float smoothing=0, attack=0, release=0, fast_attack=0, slow_release=0;
    float matching=0, damping=0, rumble=0;
    float tight=0; // tight_room as 0..1, smoothed
    float low=0;   // the lows a tight room's send leaves out
    float reduction=0; // compressor gain reduction in dB
    float limit=1;     // limiter gain
    float power_in=0, power_out=0, power_final=0, power_reference=0;
    float post_makeup=1, post_rate=0;
    bool used=false;
    MasterFx() { set_rate(44100); }
    void set_rate(unsigned rate) {
        double scale=rate/44100.0;
        for(unsigned side=0; side<2; ++side) {
            for(unsigned i=0; i<8; ++i)
                combs[side][i]=Line{std::vector<float>(std::lround((comb_frames[i]+side*spread_frames)*scale)),0,0};
            for(unsigned i=0; i<4; ++i)
                allpasses[side][i]=Line{std::vector<float>(std::lround((allpass_frames[i]+side*spread_frames)*scale)),0,0};
        }
        auto pole=[rate](double seconds) { return float(1-std::exp(-1/(seconds*rate))); };
        smoothing=pole(0.02); attack=pole(0.002); release=pole(0.15); matching=pole(3);
        fast_attack=pole(0.0002); slow_release=pole(0.25);
        post_rate=pole(1.5);
        damping=float(std::pow(0.25,1/scale));
        rumble=float(1-std::exp(-2*std::numbers::pi*180/rate));
        low=0;
        reduction=0; limit=post_makeup=1; power_in=power_out=power_final=power_reference=0;
        used=false;
    }
    void set_knob(unsigned which,uint8_t value) { knobs[which]=value; }
    void set_tight_room(bool on) { tight_room=on; }
    // True while both knobs are at zero and their smoothed values have got
    // there. A tail left in the delay lines is dropped, not kept for later.
    bool idle() {
        if(knobs[0] || knobs[1] || compress>1e-4f || reverb>1e-4f) return false;
        if(used) {
            for(auto &side : combs) for(auto &line : side) { std::fill(line.data.begin(),line.data.end(),0.f); line.store=0; }
            for(auto &side : allpasses) for(auto &line : side) std::fill(line.data.begin(),line.data.end(),0.f);
            compress=reverb=reduction=0; limit=post_makeup=1; power_in=power_out=power_final=power_reference=low=0;
            used=false;
        }
        return true;
    }
    // Samples are in 16-bit units and may exceed full scale on the way in.
    void process(float &left,float &right) {
        used=true;
        compress+=(knobs[0]/127.f-compress)*smoothing;
        reverb+=(knobs[1]/127.f-reverb)*smoothing;
        // Reverb, in two characters. The big room decays in half a second
        // to one as the knob rises and keeps its lows, so a bass drum
        // rumbles in it. The tight room is shorter (0.3 s to about half
        // a second), louder for its length, and fed without the lows below
        // 180 Hz.
        // Neither goes on to the long tails that smear drums. The tank is
        // always fed, so turning the knob up finds it ringing.
        tight+=((tight_room ? 1.f : 0.f)-tight)*smoothing;
        auto quiet=[](float value) { return std::fabs(value)<1e-15f ? 0.f : value; }; // no denormals in a dying tail
        auto blend=[this](float big,float small) { return big+(small-big)*tight; };
        float send=(left+right)*0.015f, wet[2]{};
        low=quiet(low+(send-low)*rumble);
        send-=low*tight;
        float feedback=blend(0.72f+0.1f*reverb,0.5f+0.13f*reverb);
        for(unsigned side=0; side<2; ++side) {
            for(auto &line : combs[side]) {
                float out=line.head();
                line.store=quiet(out*(1-damping)+line.store*damping);
                line.head()=send+line.store*feedback;
                line.advance();
                wet[side]+=out;
            }
            for(auto &line : allpasses[side]) {
                float buffered=line.head(), out=buffered-wet[side];
                line.head()=quiet(wet[side]+buffered*0.5f);
                line.advance();
                wet[side]=out;
            }
        }
        float mix=blend(0.6f*reverb*reverb,0.92f*reverb*std::sqrt(reverb));
        left+=wet[0]*mix; right+=wet[1]*mix;
        // At high settings, catch the attack as well as the body. A longer
        // release lets the gain rise between drum hits for an audible pump.
        // One detector drives both channels to preserve the stereo image.
        if(compress<=1e-4f) { power_in=power_out=power_final=power_reference=0; post_makeup=1; return; }
        float character=compress*compress;
        float threshold=-(24*std::sqrt(compress)+6*character);
        float slope=1-1/(1+5*compress+4*character);
        float peak=std::max(std::fabs(left),std::fabs(right));
        float over=(peak>0.03f ? 20*std::log10(peak/32768) : -120.f)-threshold;
        float target=over>0 ? over*slope : 0;
        float attack_rate=attack+(fast_attack-attack)*character;
        float release_rate=release+(slow_release-release)*character;
        reduction+=(target-reduction)*(target>reduction ? attack_rate : release_rate);
        if(reduction<1e-6f) reduction=0;
        float gain=std::pow(10.f,-reduction/20);
        // Match the compressor's output power to the incoming power.
        float power=left*left+right*right;
        power_in+=(power-power_in)*matching;
        float ref_l=std::clamp(left,-32768.f,32767.f), ref_r=std::clamp(right,-32768.f,32767.f);
        power_reference+=(ref_l*ref_l+ref_r*ref_r-power_reference)*matching;
        power_out+=(power*gain*gain-power_out)*matching;
        if(power_out<1e-9f) power_in=power_out=0;
        else gain*=std::min(std::sqrt(power_in/power_out),64.f)*post_makeup;
        // Only peaks need limiting. At high COMP settings, recover quickly
        // so a clipped transient cannot hold the following drum body down.
        float ceiling=peak*gain>32767 ? 32767/(peak*gain) : 1;
        limit=ceiling;
        left*=gain*limit; right*=gain*limit;
        power_final+=(left*left+right*right-power_final)*matching;
        if(power_reference>1e6f && power_final>1e6f) {
            float error=std::clamp(std::sqrt(power_reference/power_final)-1.f,-0.2f,0.2f);
            post_makeup=std::clamp(post_makeup*(1.f+error*post_rate),1.f,4.f);
        }
    }
};

// Boost, then the master effects, then one clip to 16 bits: what the standalone
// engine plays. With both effect knobs at zero the effects are bypassed, so the
// output is the boosted firmware stream sample for sample.
struct OutputStage {
    uint8_t output_gain=64; // Post-DSP boost: 16 is unity and 127 is almost +18 dB.
    MasterFx fx;
    void set_output_gain(uint8_t value) { output_gain=value; }
    std::array<int16_t,2> process(std::array<int16_t,2> firmware) {
        auto scale=[this](int16_t sample) {
            int value=int(sample)*int(output_gain)/16;
            return int16_t(std::clamp(value,-32768,32767));
        };
        if(fx.idle()) return {scale(firmware[0]), scale(firmware[1])};
        // The effects take the boosted signal unclipped and clip once, last.
        float left=float(firmware[0])*output_gain/16, right=float(firmware[1])*output_gain/16;
        fx.process(left,right);
        auto clip=[](float sample) { return int16_t(std::lrint(std::clamp(sample,-32768.f,32767.f))); };
        return {clip(left), clip(right)};
    }
};

#endif
