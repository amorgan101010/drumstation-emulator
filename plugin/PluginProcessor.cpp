// SPDX-License-Identifier: GPL-3.0-or-later
#include <juce_audio_utils/juce_audio_utils.h>
#include "core/board.h"
#include "core/fx.h"
#include <cstdlib>
#include <fstream>
#include <iterator>

namespace {
std::vector<uint8_t> readRom(const juce::File& file) {
    std::ifstream input(file.getFullPathName().toStdString(), std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}
juce::File romDirectory() {
    if (const auto* env = std::getenv("DRUMSTATION_ROM_DIR")) return juce::File(env);
    return juce::File::getSpecialLocation(juce::File::userApplicationDataDirectory)
        .getChildFile("DrumStation").getChildFile("roms");
}
}

class DrumStationProcessor final : public juce::AudioProcessor {
public:
    DrumStationProcessor() : AudioProcessor(BusesProperties().withOutput("Output", juce::AudioChannelSet::stereo(), true)) {
        try {
            const auto dir = romDirectory();
            board = std::make_unique<Board>(readRom(dir.getChildFile("v1.2.u15")),
                                            readRom(dir.getChildFile("v1.2.u28")));
            board->set_synthetic_eeprom(15);
            board->experimental = true;
            board->sport_log = false;
        } catch (const std::exception& e) { error = e.what(); }
    }
    const juce::String getName() const override { return "DrumStation"; }
    void prepareToPlay(double rate, int) override { sampleRate = rate > 0 ? rate : 44100.0; phase = 0; }
    void releaseResources() override {}
    bool isBusesLayoutSupported(const BusesLayout& l) const override {
        return l.getMainOutputChannelSet() == juce::AudioChannelSet::stereo() && l.getMainInputChannelSet().isDisabled();
    }
    void processBlock(juce::AudioBuffer<float>& audio, juce::MidiBuffer& midi) override {
        audio.clear();
        if (!board) return;
        auto it = midi.cbegin();
        for (int i = 0; i < audio.getNumSamples(); ++i) {
            while (it != midi.cend() && (*it).samplePosition <= i) {
                auto message = (*it).getMessage();
                for (int b = 0; b < message.getRawDataSize(); ++b)
                    board->enqueue_midi(message.getRawData()[b]);
                ++it;
            }
            phase += 44100.0 / sampleRate;
            while (phase >= 1.0) { last = output.process(board->firmware_frame()); phase -= 1.0; }
            audio.setSample(0, i, last[0] / 32768.0f);
            audio.setSample(1, i, last[1] / 32768.0f);
        }
    }
    juce::AudioProcessorEditor* createEditor() override {
        class Editor final : public juce::AudioProcessorEditor {
        public:
            explicit Editor(DrumStationProcessor& p) : juce::AudioProcessorEditor(p) {
                label.setText(p.board ? "DrumStation ready — send MIDI on channel 15" :
                    "ROMs required in " + romDirectory().getFullPathName() + " (" + p.error + ")",
                    juce::dontSendNotification);
                label.setJustificationType(juce::Justification::centred);
                addAndMakeVisible(label);
                setSize(640, 120);
            }
            void resized() override { label.setBounds(getLocalBounds().reduced(12)); }
        private:
            juce::Label label;
        };
        return new Editor(*this);
    }
    bool hasEditor() const override { return true; }
    double getTailLengthSeconds() const override { return 0; }
    bool acceptsMidi() const override { return true; }
    bool producesMidi() const override { return false; }
    bool isMidiEffect() const override { return false; }
    int getNumPrograms() override { return 1; }
    int getCurrentProgram() override { return 0; }
    void setCurrentProgram(int) override {}
    const juce::String getProgramName(int) override { return {}; }
    void changeProgramName(int, const juce::String&) override {}
    void getStateInformation(juce::MemoryBlock&) override {}
    void setStateInformation(const void*, int) override {}
private:
    std::unique_ptr<Board> board;
    juce::String error;
    double sampleRate = 44100.0, phase = 0;
    std::array<int16_t, 2> last{};
    OutputStage output;
};

juce::AudioProcessor* JUCE_CALLTYPE createPluginFilter() { return new DrumStationProcessor(); }
