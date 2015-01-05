'''
Module for writing hdf5 APS2 files from LL's and patterns

Copyright 2014 Raytheon BBN Technologies

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import h5py
import os
import numpy as np
from warnings import warn
from copy import copy
import Compiler, ControlFlow
import PatternUtils
import APSPattern

#Some constants
ADDRESS_UNIT = 4 #everything is done in units of 4 timesteps
MIN_ENTRY_LENGTH = 8
MAX_WAVEFORM_PTS = 2**28 #maximum size of waveform memory
MAX_WAVEFORM_VALUE = 2**13-1 #maximum waveform value i.e. 14bit DAC
MAX_NUM_INSTRUCTIONS = 2**26
MAX_REPEAT_COUNT = 2**16-1;
MAX_TRIGGER_COUNT = 2**32-1

# instruction encodings
WFM    = 0x0
MARKER = 0x1
WAIT   = 0x2
LOAD   = 0x3
REPEAT = 0x4
CMP    = 0x5
GOTO   = 0x6
CALL   = 0x7
RET    = 0x8
SYNC   = 0x9
PFETCH = 0xA
WAITCMP = 0XB

# WFM/MARKER op codes
PLAY      = 0x0
WAIT_TRIG = 0x1
WAIT_SYNC = 0x2
WFM_OP_OFFSET = 46
TA_PAIR_BIT   = 45

# CMP op encodings
EQUAL       = 0x0
NOTEQUAL    = 0x1
GREATERTHAN = 0x2
LESSTHAN    = 0x3

def create_wf_vector(wfLib):
	'''
	Helper function to create the wf vector and offsets into it.
	'''
	max_pts_needed = 0
	for wf in wfLib.values():
		if len(wf) == 1:
			max_pts_needed += ADDRESS_UNIT
		else:
			max_pts_needed += len(wf)

	wfVec = np.zeros(max_pts_needed, dtype=np.int16)
	offsets = {}
	idx = 0
	for key, wf in wfLib.items():
		#Clip the wf
		wf[wf>1] = 1.0
		wf[wf<-1] = -1.0
		#TA pairs need to be repeated ADDRESS_UNIT times
		if wf.size == 1:
			wf = wf.repeat(ADDRESS_UNIT)
		#Ensure the wf is an integer number of ADDRESS_UNIT's 
		trim = wf.size%ADDRESS_UNIT
		if trim:
			wf = wf[:-trim]
		assert idx + wf.size < MAX_WAVEFORM_PTS, 'Oops! You have exceeded the waveform memory of the APS'
		wfVec[idx:idx+wf.size] = np.uint16(np.round(MAX_WAVEFORM_VALUE*wf))
		offsets[key] = idx
		idx += wf.size 
					
	#Trim the waveform 
	wfVec.resize(idx)

	return wfVec, offsets

def calc_marker_delay(entry):
	#The firmware cannot handle 0 delay markers so push out one clock cycle
	if entry.markerDelay1 is not None:
		if entry.markerDelay1 < ADDRESS_UNIT:
			entry.markerDelay1 = ADDRESS_UNIT
		markerDelay1 = entry.markerDelay1//ADDRESS_UNIT
	else:
		markerDelay1 = 0

	if entry.markerDelay2 is not None:
		if entry.markerDelay2 < ADDRESS_UNIT:
			entry.markerDelay2 = ADDRESS_UNIT
		markerDelay2 = entry.markerDelay2//ADDRESS_UNIT
	else:
		markerDelay2 = 0

	return markerDelay1, markerDelay2

class Instruction(object):
	def __init__(self, header, payload, label=None, target=None):
		self.header = header
		self.payload = payload
		self.label = label
		self.target = target

	@classmethod
	def unflatten(cls, instr):
		return cls(header = int(long(instr) >> 56) & 0xff, payload = long(instr) & 0xffffffffffffff)

	def __repr__(self):
		return self.__str__()

	def __str__(self):
		labelPart = "{0}: ".format(self.label) if self.label else ""
		out = labelPart + "Instruction(" + str(hex(self.header)) + ", "
		if self.target:
			out += str(self.target) + "/"
		return out + str(hex(self.payload)) + ")"

	@property
	def address(self):
		return self.payload & 0xffffffff # bottom 32-bits of payload

	@address.setter
	def address(self, value):
		self.payload |= value & 0xffffffff

	@property
	def writeFlag(self):
		return self.header & 0x1

	@writeFlag.setter
	def writeFlag(self, value):
		self.header |= value & 0x1

	@property
	def opcode(self):
		return self.header >> 4

	def flatten(self):
		return long((self.header << 56) | self.payload)

def Waveform(addr, count, isTA, write=False, label=None):
	header = (WFM << 4) | (write & 0x1)
	count = int(count)
	count = ((count // ADDRESS_UNIT)-1) & 0x000fffff # 20 bit count
	addr = (addr // ADDRESS_UNIT) & 0x00ffffff # 24 bit addr
	payload = (PLAY << WFM_OP_OFFSET) | ((isTA & 0x1) << TA_PAIR_BIT) | (count << 24) | addr
	return Instruction(header, payload, label)

def Marker(sel, state, count, write=False, label=None):
	header = (MARKER << 4) | ((sel & 0x3) << 2) | (write & 0x1)
	count = int(count)
	four_count = ((count // ADDRESS_UNIT)-1) & 0xffffffff # 32 bit count
	count_rem = count % ADDRESS_UNIT
	if state == 0:
		transitionWords = {0: 0b0000, 1: 0b1000, 2: 0b1100, 3: 0b1110}
		transition = transitionWords[count_rem]
	else:
		transitionWords = {0: 0b1111, 1: 0b0111, 2: 0b0011, 3: 0b0001}
		transition = transitionWords[count_rem]
	payload = (PLAY << WFM_OP_OFFSET) | (transition << 33) | ((state & 0x1) << 32) | four_count
	return Instruction(header, payload, label)

def Command(cmd, payload, write=False, label=None):
	header = (cmd << 4)
	if isinstance(payload, (int, long)):
		instr = Instruction(header, payload, label)
	else:
		instr = Instruction(header, 0, label, target=payload)
	instr.writeFlag = write
	return instr

def Sync(label=None):
	return Command(SYNC, WAIT_SYNC << WFM_OP_OFFSET, write=True, label=label)

def Wait(label=None):
	return Command(WAIT, WAIT_TRIG << WFM_OP_OFFSET, write=True, label=label)

def WaitCmp(label=None):
	return Command(WAITCMP, 0, label=label)

def Cmp(op, mask, label=None):
	return Command(CMP, (op << 8) | (mask & 0xff), label=label)

def Goto(addr, label=None):
	return Command(GOTO, addr, label=label)

def Call(addr, label=None):
	return Command(CALL, addr, label=label)

def Return(label=None):
	return Command(RET, 0, label=label)

def Load(count, label=None):
	return Command(LOAD, count, label=label)

def Repeat(addr, label=None):
	return Command(REPEAT, 0, label=label)

def timestamp_entries(seq):
	t = 0
	for ct in range(len(seq)):
		seq[ct].startTime = t
		t += seq[ct].totLength

def create_seq_instructions(seqs, offsets):
	'''
	Helper function to create instruction vector from an IR sequence and an offset dictionary
	keyed on the wf keys.

	Seqs is a list of lists containing waveform and marker data, e.g.
	[wfSeq, m1Seq, m2Seq, m3Seq, m4Seq]

	We take the strategy of greedily grabbing the next instruction that occurs in time, accross
	all	waveform and marker channels.
	'''

	# timestamp all entries before filtering (where we lose time information on control flow)
	for seq in seqs:
		timestamp_entries(seq)

	# filter out sequencing instructions from the waveform and marker lists, so that seqs becomes:
	# [control-flow, wfs, m1, m2, m3, m4]
	controlInstrs = filter(lambda s: isinstance(s, ControlFlow.ControlInstruction), seqs[0])
	seqs.insert(0, controlInstrs)
	for ct in range(1, len(seqs)):
		seqs[ct] = filter(lambda s: isinstance(s, Compiler.LLWaveform), seqs[ct])

	# create (seq, startTime) pairs over all sequences
	timeTuples = []
	for ct, seq in enumerate(seqs):
		timeTuples += [(entry.startTime, ct) for entry in seq]
	timeTuples.sort()

	# keep track of where we are in each sequence
	curIdx = np.zeros(len(seqs), dtype=np.int64)
	
	cmpTable = {'==': EQUAL, '!=': NOTEQUAL, '>': GREATERTHAN, '<': LESSTHAN}

	# always start with SYNC (stealing label from first pulse)
	firstLabel = seqs[timeTuples[0][1]][0].label
	instructions = [Sync(label=firstLabel)]

	while len(timeTuples) > 0:
		startTime, curSeq = timeTuples.pop(0)
		entry = seqs[curSeq][curIdx[curSeq]]
		nextStartTime = timeTuples[0][0] if len(timeTuples) > 0 else -1
		writeFlag = (startTime != nextStartTime)
		curIdx[curSeq] += 1

		# poor man's way of deciding waveform or marker is to use curSeq
		if curSeq == 1: # waveform channel
			if entry.length < MIN_ENTRY_LENGTH:
				continue
			instructions.append(Waveform(offsets[entry.key],
				                         entry.length,
				                         entry.isTimeAmp,
				                         write=writeFlag,
				                         label=entry.label))
		elif curSeq > 1: # a marker channel
			if entry.length < MIN_ENTRY_LENGTH:
				continue
			markerSel = curSeq - 2
			state = (entry.key != PatternUtils.TAZKey)
			instructions.append(Marker(markerSel,
				                       state,
				                       entry.length,
				                       write=writeFlag,
				                       label=entry.label))

		else: # otherwise we are dealing with control-flow
			# zero argument commands
			if entry.instruction == 'WAIT':
				instructions.append(Wait(label=entry.label))
			elif entry.instruction == 'WAITCMP':
				instructions.append(WaitCmp(label=entry.label))
			elif entry.instruction == 'SYNC':
				instructions.append(Sync(label=entry.label))
			elif entry.instruction == 'RETURN':
				instructions.append(Return(label=entry.label))
			# target argument commands
			elif entry.instruction == 'GOTO':
				instructions.append(Goto(entry.target, label=entry.label))
			elif entry.instruction == 'CALL':
				instructions.append(Call(entry.target, label=entry.label))
			elif entry.instruction == 'REPEAT':
				instructions.append(Call(entry.target, label=entry.label))
			# value argument commands
			elif entry.instruction == 'LOAD':
				instructions.append(Load(entry.value-1, label=entry.label))
			elif entry.instruction == 'CMP':
				instructions.append(Cmp(cmpTable[entry.operator], entry.mask, label=entry.label))

	return instructions

def create_instr_data(seqs, offsets):
	'''
	Constructs the complete instruction data vector, and does basic checks for validity.
	'''
	maxlen = max([len(s) for s in seqs])
	instructions = []
	for ct in range(maxlen):
		instructions += create_seq_instructions([s[ct] if ct < len(s) else [] for s in seqs], offsets)

	resolve_symbols(instructions)

	if instructions[-1] != Goto(0):
		instructions.append(Goto(0))

	assert len(instructions) < MAX_NUM_INSTRUCTIONS, 'Oops! too many instructions: {0}'.format(len(instructions))

	data = np.array([instr.flatten() for instr in instructions], dtype=np.uint64)
	return data

def resolve_symbols(seq):
	symbols = {}
	# create symbol look-up table
	for ct, entry in enumerate(seq):
		if entry.label and entry.label not in symbols:
			symbols[entry.label] = ct
	# then update
	for entry in seq:
		if entry.target:
			noOffsetLabel = copy(entry.target)
			noOffsetLabel.offset = 0
			entry.address = symbols[noOffsetLabel] + entry.target.offset

def compress_marker(markerLL):
	'''
	Compresses adjacent entries of the same state into single entries
	'''
	for seq in markerLL:
		idx = 0
		while idx+1 < len(seq):
			if (isinstance(seq[idx], Compiler.LLWaveform)
				and isinstance(seq[idx+1], Compiler.LLWaveform)
				and seq[idx].key == seq[idx+1].key):

				# TODO: handle repeats != 0 ?
				seq[idx].length += seq[idx+1].length
				del seq[idx+1]
			else:
				idx += 1

def write_APS2_file(awgData, fileName):
	'''
	Main function to pack channel LLs into an APS h5 file.
	'''

	#Preprocess the LL data to handle APS restrictions
	# seqs = [APSPattern.preprocess_APS(seq, awgData['ch12']['wfLib']) for seq in awgData['ch12']['linkList']]

	# compress marker data
	for field in ['ch12m1', 'ch12m2', 'ch12m3', 'ch12m4']:
		if 'linkList' in awgData[field].keys():
			compress_marker(awgData[field]['linkList'])
		else:
			awgData[field]['linkList'] = []

	#Create the waveform vectors
	wfInfo = []
	wfInfo.append(create_wf_vector({key:wf.real for key,wf in awgData['ch12']['wfLib'].items()}))
	wfInfo.append(create_wf_vector({key:wf.imag for key,wf in awgData['ch12']['wfLib'].items()}))

	# build instruction vector
	instructions = create_instr_data([awgData[s]['linkList'] for s in ['ch12', 'ch12m1', 'ch12m2', 'ch12m3', 'ch12m4']],
		wfInfo[0][1])

	#Open the HDF5 file
	if os.path.isfile(fileName):
		os.remove(fileName)
	with h5py.File(fileName, 'w') as FID:  
		FID['/'].attrs['Version'] = 3.0
		FID['/'].attrs['channelDataFor'] = np.uint16([1,2])

		#Create the groups and datasets
		for chanct in range(2):
			chanStr = '/chan_{0}'.format(chanct+1)
			chanGroup = FID.create_group(chanStr)
			#Write the waveformLib to file
			FID.create_dataset(chanStr+'/waveforms', data=wfInfo[chanct][0])

			#Write the instructions to channel 1
			if np.mod(chanct,2) == 0:
				FID.create_dataset(chanStr+'/instructions', data=instructions)

def read_APS2_file(fileName):
	chanStrs = ['ch1', 'ch2', 'ch12m1', 'ch12m2', 'ch12m3', 'ch12m4']
	seqs = {ch: [] for ch in chanStrs}
	with h5py.File(fileName, 'r') as FID:
		ch1wf = (1.0/MAX_WAVEFORM_VALUE)*FID['/chan_1/waveforms'].value.flatten()
		ch2wf = (1.0/MAX_WAVEFORM_VALUE)*FID['/chan_2/waveforms'].value.flatten()
		instructions = FID['/chan_1/instructions'].value.flatten()

		for data in instructions:
			instr = Instruction.unflatten(data)
			if instr.opcode == WAIT:
				for ch in chanStrs:
					seqs[ch].append(np.array([], dtype=np.float64))
			elif instr.opcode == WFM:
				addr = (instr.payload & 0x00ffffff) * ADDRESS_UNIT
				count = (instr.payload >> 24) & 0xfffff
				count = (count + 1) * ADDRESS_UNIT
				isTA = (instr.payload >> 45) & 0x1
				if not isTA:
					seqs['ch1'][-1] = np.append( seqs['ch1'][-1], ch1wf[addr:addr + count] )
					seqs['ch2'][-1] = np.append( seqs['ch2'][-1], ch2wf[addr:addr + count] )
				else:
					seqs['ch1'][-1] = np.append( seqs['ch1'][-1], np.array([ch1wf[addr]] * count) )
					seqs['ch2'][-1] = np.append( seqs['ch2'][-1], np.array([ch2wf[addr]] * count) )
			elif instr.opcode == MARKER:
				chan = 'ch12m' + str(((instr.header >> 2) & 0x3) + 1)
				count = instr.payload & 0xffffffff
				count = (count + 1) * ADDRESS_UNIT
				state = (instr.payload >> 32) & 0x1
				seqs[chan][-1] = np.append( seqs[chan][-1], np.array([state] * count) )

	return seqs
