// SPDX-License-Identifier: BSD-3-Clause
// Derived from MAME MN1880, copyright AJR. Framework adaptation for DrumStation.
#pragma once
#include <cstdint>
#include <functional>
#include <utility>
#include <bit>
#include <cstdio>
#include <stdexcept>
using u8=uint8_t; using u16=uint16_t; using u32=uint32_t; using s8=int8_t; using s16=int16_t; using s32=int32_t; using offs_t=uint32_t;
template<typename T> constexpr u32 BIT(T v, unsigned n) { return (u32(v)>>n)&1; }
class mn1880_device {
public:
    mn1880_device();
    void device_reset();
    void execute_run();
    void run(int cycles) { m_icount=cycles; execute_run(); }
    std::function<void(u16)> trace;
    void debugger_instruction_hook(u16 pc) { if(trace) trace(pc); }
    int standard_irq_callback(unsigned, u16) { return 0; }
    template<typename... Args> void logerror(const char *fmt, Args... args) { std::fprintf(stderr,fmt,args...); }
	struct cpu_registers
	{
		cpu_registers() : ip(0), irp(0), ir(0), fs(0), xp(0), yp(0), sp(0), lp(0), wait(0), ie(0), iemask(false) { }

		u8 addcz(u8 data1, u8 data2, bool carry, bool holdz);
		u8 adddcz(u8 data1, u8 data2, bool carry);
		u8 subcz(u8 data1, u8 data2, bool carry, bool holdz);
		u8 subdcz(u8 data1, u8 data2, bool carry);
		u8 rolc(u8 data);
		u8 rorc(u8 data);
		u8 asrc(u8 data);
		void branch(u16 label);

		u16 ip;
		u16 irp;
		u8 ir;
		u8 fs;
		u16 xp;
		u16 yp;
		u16 sp;
		u16 lp;
		u16 wait;
		u16 ie;
		bool iemask;
	};

	enum class microstate : u8 {
		NEXT,
		NOP_1,
		REP_1,
		CLRSET_1,
		T1B_1,
		MOVL31_1, MOVL31_2,
		MOVL34_1, MOVL34_2, MOVL34_3, MOVL34_4,
		MOVL35_1, MOVL35_2, MOVL35_3, MOVL35_4,
		MOV36_1, MOV36_2,
		MOV37_1,
		MOVL38_1, MOVL38_2, MOVL38_3,
		MOVL39_1, MOVL39_2, MOVL39_3,
		ASL_1, ASL_2,
		ASR_1, ASR_2,
		DEC40_1,
		NOT_1,
		CMPM44_1, CMPM44_2, CMPM44_3,
		CMPM45_1, CMPM45_2,
		XCH4_1, XCH4_2,
		INC48_1,
		CLR4A_1,
		ROL_1, ROL_2,
		ROR_1, ROR_2,
		CMPM50_1, CMPM50_2, CMPM50_3,
		CMPM52_1, CMPM52_2,
		DIV51_1, DIV51_2, DIV51_3, DIV51_4, DIV51_5, DIV51_6, DIV51_7, DIV51_8, DIV51_9, DIV51_10,
		MOVDA_1, MOVDA_2, MOVDA_3,
		MOV54_1,
		MOV55_1,
		MOV56_1, MOV56_2,
		XCH58_1, XCH58_2, XCH58_3, XCH58_4,
		MUL59_1, MUL59_2, MUL59_3, MUL59_4, MUL59_5, MUL59_6, MUL59_7, MUL59_8,
		MOVL5C_1, MOVL5C_2, MOVL5C_3,
		MOVL5D_1, MOVL5D_2,
		MOVL5E_1, MOVL5E_2, MOVL5E_3, MOVL5E_4,
		MOVL5F_1, MOVL5F_2,
		CMP_1, CMP_2, CMP_3,
		AND_1, AND_2, AND_3,
		XOR_1, XOR_2, XOR_3,
		OR_1, OR_2, OR_3,
		SUBC_1, SUBC_2, SUBC_3,
		SUBD_1, SUBD_2, SUBD_3, SUBD_4,
		ADDC_1, ADDC_2, ADDC_3,
		ADDD_1, ADDD_2, ADDD_3, ADDD_4,
		BR80_1, BR88_1,
		CMPL_1, CMPL_2, CMPL_3, CMPL_4, CMPL_5,
		CLRFS_1, SETFS_1,
		CALL90_1, CALL90_2,
		BRA0_1,
		POPFS_1, POPFS_2,
		POPB4_1, POPB4_2, POPB4_3,
		POPB5_1, POPB5_2,
		POPB7_1,
		PUSHFS_1,
		PUSHBC_1, PUSHBC_2, PUSHBC_3,
		PUSHBD_1, PUSHBD_2, PUSHBD_3,
		PUSHBF_1,
		SUBCL_1, SUBCL_2, SUBCL_3, SUBCL_4, SUBCL_5, SUBCL_6,
		DIVC1_1, DIVC1_2,
		XCHC4_1, XCHC4_2,
		XCHC5_1, XCHC5_2,
		ADDCL_1, ADDCL_2, ADDCL_3, ADDCL_4, ADDCL_5, ADDCL_6,
		MULC9_1, MULC9_2, MULC9_3,
		MOVCC_1,
		MOVCD_1, MOVCD_2,
		CMPD0_1, CMPD0_2,
		CMPD1_1, CMPD1_2,
		XCHD4_1, XCHD4_2,
		XCHD7_1,
		MOVD8_1,
		MOVD9_1,
		MOVDC_1,
		MOVDD_1,
		LOOP_1, LOOP_2,
		DECE4_1,
		INCE5_1,
		ADDRE8_1, ADDRE8_2, ADDRE8_3,
		ADDRE9_1, ADDRE9_2, ADDRE9_3,
		ADDREC_1,
		ADDRED_1,
		CMPBF0_1,
		CMPBF1_1, CMPBF1_2, CMPBF1_3,
		MOV1_1, MOV1_2, MOV1_3, MOV1_4, MOV1N_4,
		WAIT_1, WAIT_2,
		RET_1, RET_2, RET_3,
		RETI_1, RETI_2, RETI_3, RETI_4, RETI_5,
		BR_1, BR_2,
		CALL_1, CALL_2, CALL_3,
		PUSHFB_1,
		BRFC_1,
		CALLFD_1,
		RDTBL_1, RDTBL_2, RDTBL_3,
		PI_1, PI_2, PI_3, PI_4,
		UNKNOWN
	};

	static void setl(u16 &pr, u8 data) { pr = (pr & 0xff00) | data; }
	static void seth(u16 &pr, u8 data) { pr = (pr & 0x00ff) | (data << 8); }

	cpu_registers &get_active_cpu() { return m_cpu[BIT(m_cpum, 4)]; }
	const cpu_registers &get_active_cpu() const { return m_cpu[BIT(m_cpum, 4)]; }
	bool output_queued() const { return m_output_queue_state != 0xff; }
	void set_output_queued() { m_output_queue_state = BIT(m_cpum, 4); }

	offs_t mmu_psen_translate(u16 addr) const { return BIT(m_mmu_enable, 6) && BIT(addr + 0x4000, 15) ? u32(m_mmu_bank[BIT(addr, 15)]) << 14 | (addr & 0x3fff) : addr; }
	offs_t mmu_data_translate(u16 addr) const { return BIT(m_mmu_enable, 7) && BIT(addr + 0x4000, 15) ? u32(m_mmu_bank[BIT(addr, 15) + 2]) << 14 | (addr & 0x3fff) : addr; }

	u8 ie0_r();
	void ie0_w(u8 data);
	u8 ie1_r();
	void ie1_w(u8 data);
	u8 cpum_r();
	void cpum_w(u8 data);

	u8 mmu_bank_r(offs_t offset);
	void mmu_bank_w(offs_t offset, u8 data);
	u8 mmu_enable_r();
	void mmu_enable_w(u8 data);

	void swap_cpus();
	void next_instruction(u8 input);

	static const microstate s_decode_map[256];
	static const u16 s_input_queue_map[16];
	static const u8 s_branch_fs[4];

    struct Bus {
        std::function<u8(u16)> read_byte;
        std::function<void(u16,u8)> write_byte;
    } m_cache, m_data;
    bool m_has_mmu = false;

	// execution state
	cpu_registers m_cpu[2];
	u8 m_cpum;
	microstate m_ustate;
	u16 m_da;
	u16 m_tmp1;
	u16 m_tmp2;
	u8 m_output_queue_state;
	s32 m_icount;

	// interrupt state
	u16 m_if;
	u16 m_irq;

	// MMU state
	u8 m_mmu_bank[4];
	u8 m_mmu_enable;
};
