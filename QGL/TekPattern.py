"""
TekPattern for creating Tek AWG files. Based off of the Matlab version.

Created on Tue May  8 20:39:26 2012

Copyright 2013 Raytheon BBN Technologies

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import struct
import io
import h5py
import numpy as np

MAX_WAVEFORM_VALUE = 2**13-1 #maximum waveform value i.e. 14bit DAC


def write_field(FID, fieldName, data, dataType):
    typeSizes = {'int16':2, 'int32':4, 'double':8, 'uint128':16}
    formatChars = {'int16':'<h', 'int32':'<i', 'double':'<d'}

    if dataType == 'char':
        dataSize = len(data)+1
        data = data+chr(0)
    else:
        dataSize = typeSizes[dataType]
        
    FID.write(struct.pack('<II', len(fieldName)+1, dataSize))
    FID.write(fieldName+chr(0))
    if dataType == 'char':
        FID.write(data)
    elif dataType == 'uint128':
        #struct doesn't support uint128 so write two 64bits
        #there are smarter ways but we really only need this for the fake timestamp
        FID.write(struct.pack('<QQ', 0, data))
    else:
        FID.write(struct.pack(formatChars[dataType], data))

def read_field(FID):
    fieldNameLen, dataSize = struct.unpack('<II', FID.read(8))
    fieldName = FID.read(fieldNameLen)[:-1]
    data = FID.read(dataSize)

    return fieldName, data
    
def pack_waveform(analog, marker1, marker2):
    '''
    Helper function to convert a floating point analog channel and two logical marker channel to a sequence of 16bit integers.
    AWG 5000 series binary data format
    m2 m1 d14 d13 d12 d11 d10 d9 d8 d7 d6 d5 d4 d3 d2 d1
    16-bit format with markers occupying left 2 bits followed by the 14 bit
    analog channel value
    '''

    #Convert decimal shape on [-1,1] to binary on [0,2^14 (16383)] 
    #AWG actually makes 111,111,111,111,10 the 100% output, and
    # 111,111,111,111,11 is one step larger than 100% output so we
    # ignore the one extra positive number and scale from [0,16382]
    analog[analog>1] = 1.0
    analog[analog<-1] = -1.0
    if len(marker1) < len(analog):
        marker1 = np.append(marker1, np.ones(len(analog) - len(marker1), dtype=np.uint16))
    if len(marker2) < len(analog):
        marker2 = np.append(marker2, np.ones(len(analog) - len(marker2), dtype=np.uint16))
    binData = np.uint16( MAX_WAVEFORM_VALUE*analog + MAX_WAVEFORM_VALUE );
    
    binData += 2**14*marker1 + 2**15*marker2
    
    return binData

def marker_waveform(LL):
    '''
    from a marker link list, construct a marker waveform
    '''
    # interpret a change in key as a state switch
    switchPts = []
    curIndex = 0
    for curEntry, nextEntry in zip(LL[:-1], LL[1:]):
        curIndex += curEntry.length*curEntry.repeat
        if curEntry.key != nextEntry.key:
            switchPts.append(curIndex)

    wfLengths = np.diff(switchPts)

    wf = np.array([], dtype=np.uint16)
    #alternate between zeros and ones
    stateHigh = False
    for l in wfLengths:
        if stateHigh:
            stateHigh = False
            wf = np.append(wf, np.ones(l, dtype=np.uint16))
        else:
            stateHigh = True
            wf = np.append(wf, np.zeros(l, dtype=np.uint16))

    return wf

def merge_waveform(n, chAB, chAm1, chAm2, chBm1, chBm2):
    '''
    Builds packed I and Q waveforms from the nth mini LL, merging in marker data.
    '''
    wfAB = np.array([], dtype=np.complex)
    for entry in chAB['linkList'][n % len(chAB['linkList'])]:
        if not entry.isTimeAmp:
            wfAB = np.append(wfAB, chAB['wfLib'][entry.key])
        else:
            wfAB = np.append(wfAB, chAB['wfLib'][entry.key][0] * np.ones(entry.length * entry.repeat))

    wfA = pack_waveform(np.real(wfAB), marker_waveform(chAm1['linkList'][n % len(chAm1['linkList'])]), marker_waveform(chAm2['linkList'][n % len(chAm2['linkList'])]))
    wfB = pack_waveform(np.imag(wfAB), marker_waveform(chBm1['linkList'][n % len(chBm1['linkList'])]), marker_waveform(chBm2['linkList'][n % len(chBm2['linkList'])]))

    return wfA, wfB

def write_waveform(FID, WFname, WFnumber, data):
    '''
    Helper function to write a waveform
    '''
    numString = str(WFnumber)
    
    write_field(FID, 'WAVEFORM_NAME_'+numString, WFname, 'char')
    
    #Set integer format
    write_field(FID, 'WAVEFORM_TYPE_'+numString, 1, 'int16')

    write_field(FID, 'WAVEFORM_LENGTH_'+numString, data.size, 'int32')
    
    write_field(FID, 'WAVEFORM_TIMESTAMP_'+numString, 0, 'uint128')
    tmpString = 'WAVEFORM_DATA_'+numString+chr(0)
    dataSize = 2*data.size     
    FID.write(struct.pack('<II', len(tmpString), dataSize))
    FID.write(tmpString)
    FID.write(data.tostring())
        

def write_Tek_file(awgData, fileName, seqName, options=None):
    '''
    Main function for writing a AWG format file.
    awgData is a nested dict with the following structure:
        {ch12: {wfLib: [...], linkList: [...] },
         ch34: {wfLib: [...], linkList: [...] },
         ch1m1: {linkList: [...]},
         ch1m2: {linkList: [...]},
         ...
         ch4m2: {linkList: [...]}, }
    '''     

    #Set the default options
    #Marker levels default to 1V. 
    if options is None:
        options = {'markerLevels': {}}
    for chanct in range(1,5):
        for markerct in range(1,3):
            tmpStr = 'ch{0}m{1}'.format(chanct,markerct)
            if tmpStr not in options['markerLevels']:
                options['markerLevels'][tmpStr] = {}
                options['markerLevels'][tmpStr]['low'] = 0.0
                options['markerLevels'][tmpStr]['high'] = 1.0

    numSeqs = max(len(awgData['ch12']['linkList']), len(awgData['ch34']['linkList']))
    
    #Open the file
    FID = io.open(fileName, 'wb')

    #Write the necessary MAGIC and VERSION fields
    write_field(FID, 'MAGIC', 5000, 'int16')
    write_field(FID, 'VERSION', 1, 'int16')

    
    #Default to the fastest sampling rate
    write_field(FID, 'SAMPLING_RATE', 1.2e9, 'double')
    
    #Run mode (1 = continuous, 2 = triggered, 3 = gated, 4 = sequence)
    #If we only have one step then there is no sequence
    runMode = 2 if numSeqs == 1 else 4
    write_field(FID, 'RUN_MODE', runMode, 'int16')

    #Default to off state
    write_field(FID, 'RUN_STATE', 0, 'int16')
    
    #Set the reference source (1: internal; 2: external)
    write_field(FID, 'REFERENCE_SOURCE', 2, 'int16')

    #Trigger threshold
    write_field(FID, 'TRIGGER_INPUT_THRESHOLD', 1.0, 'double')    
    
    #Marker's to high/low (1 = amp/offset, 2 = high/low)
    for chanct in range(1,5):
        chanStr = str(chanct)
        write_field(FID, 'CHANNEL_STATE_'+chanStr, 1, 'int16')
        write_field(FID, 'MARKER1_METHOD_'+chanStr, 2, 'int16')
        write_field(FID, 'MARKER1_LOW_'+chanStr, options['markerLevels']['ch'+chanStr+'m1']['low'], 'double')
        write_field(FID, 'MARKER1_HIGH_'+chanStr, options['markerLevels']['ch'+chanStr+'m1']['high'], 'double')
        write_field(FID, 'MARKER2_METHOD_'+chanStr, 2, 'int16')
        write_field(FID, 'MARKER2_LOW_'+chanStr, options['markerLevels']['ch'+chanStr+'m2']['low'], 'double')
        write_field(FID, 'MARKER2_HIGH_'+chanStr, options['markerLevels']['ch'+chanStr+'m2']['high'], 'double')
        
    #If we have only one step then we specify the waveform names
    if numSeqs == 1:
        for chanct in range(1,5):
            write_field(FID, 'OUTPUT_WAVEFORM_NAME_'+str(chanct), seqName+'Ch'+str(chanct)+'001', 'char')
    
    #Now write the waveforms
    for ct in range(numSeqs):
        wf1, wf2 = merge_waveform(ct, awgData['ch12'], awgData['ch1m1'], awgData['ch1m2'], awgData['ch2m1'], awgData['ch2m2'])
        write_waveform(FID, '{0}Ch1{1:03d}'.format(seqName, ct+1), 4*ct+1, wf1)
        write_waveform(FID, '{0}Ch2{1:03d}'.format(seqName, ct+1), 4*ct+2, wf2)

        wf3, wf4 = merge_waveform(ct, awgData['ch34'], awgData['ch3m1'], awgData['ch3m2'], awgData['ch4m1'], awgData['ch4m2'])
        write_waveform(FID, '{0}Ch3{1:03d}'.format(seqName, ct+1), 4*ct+3, wf3)
        write_waveform(FID, '{0}Ch4{1:03d}'.format(seqName, ct+1), 4*ct+4, wf4)
    
    #Write the sequence table
    for seqct in range(1,numSeqs+1):
        ctStr = str(seqct)
        #We wait for a trigger at every sequence
        write_field(FID, 'SEQUENCE_WAIT_'+ctStr, 1, 'int16')
        write_field(FID, 'SEQUENCE_JUMP_'+ctStr, 0, 'int16')
        write_field(FID, 'SEQUENCE_LOOP_'+ctStr, 1, 'int32')
        
        #If we are on the final one then set the goto back to the beginning
        goto = 1 if seqct == numSeqs else 0
        write_field(FID, 'SEQUENCE_GOTO_'+ctStr, goto, 'int16')
        
        for chanct in range(1,5):
            WFname = '{0}Ch{1}{2:03d}'.format(seqName, chanct, seqct)
            write_field(FID, 'SEQUENCE_WAVEFORM_NAME_CH_'+str(chanct)+'_'+ctStr, WFname, 'char')
            
    FID.close()
    
def read_Tek_file(fileName):
    '''
    Helper function to read in TekAWG h5 dump for plotting.
    '''
    AWGData = {}
    waveformMask = 2**14-1;
    marker1Mask = 2**14;
    marker2Mask = 2**15;
    
    with h5py.File(fileName, 'r') as FID:
        for chanct in range(1,5):
            chanStr = 'ch{0}'.format(chanct)
            marker1Str = 'ch{0}m1'.format(chanct)
            marker2Str = 'ch{0}m2'.format(chanct)
            if chanStr in list(FID):
                AWGData[chanStr] = [(1.0/MAX_WAVEFORM_VALUE)*(np.int16(tmpSeq&waveformMask)-MAX_WAVEFORM_VALUE-1) for tmpSeq in FID[chanStr]]
                AWGData[marker1Str] = [tmpSeq&marker1Mask == marker1Mask for tmpSeq in FID[chanStr]];
                AWGData[marker2Str] = [tmpSeq&marker2Mask == marker2Mask for tmpSeq in FID[chanStr]];
    
    return AWGData

def read_Tek_awg_file(fileName):
    '''
    Helper function to read in .awg file
    '''
    AWGData = {}
    waveformMask = 2**14-1;
    marker1Mask = 2**14;
    marker2Mask = 2**15;

    with io.open(fileName, 'rb') as FID:
        name, data = read_field(FID)
        data = struct.unpack('<h', data)[0]
        assert name == 'MAGIC' and data == 5000, "Invalid file (MAGIC number not found)"
        name, data = read_field(FID)
        data = struct.unpack('<h', data)[0]
        assert name == 'VERSION' and data == 1, "Invalid file version"

        while 1:
            name, data = read_field(FID)
            if 'WAVEFORM_DATA' in name:
                wf = np.fromstring(data, dtype=np.uint16)
                AWGData[name] = wf & waveformMask
                AWGData[name + 'm1'] = wf & marker1Mask
                AWGData[name + 'm2'] = wf & marker2Mask

            # check if there is more to read
            b = FID.read(1)
            if b == '':
                break
            else:
                FID.seek(-1,1)

    return AWGData
    
if __name__ == '__main__':

    pass
        
    
