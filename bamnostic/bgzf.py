from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

"""Modified version of BioPython.bgzf module. Includes LRU buffer dictionary.
Copyright (c) 2010-2015 by Peter Cock.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Description: Read and write BGZF compressed files (the GZIP variant used in BAM).
Significant changes were made to the original BGZF module, produced by 
Peter Cock. Aside from adding an LRU dictionary, the new BGZF module can read
BAM files directly, decompressing and unpacking the byte-encoded data structure
outlined in the BAM_ format. 

.. _BAM: https://samtools.github.io/hts-specs/SAMv1.pdf

"""

import sys
import zlib
import struct
import io
import os
import warnings

import bamnostic
from bamnostic.utils import *

_PY_VERSION = sys.version

if _PY_VERSION.startswith('2'):
    from io import open


def format_warnings(message, category, filename, lineno, file=None, line=None):
    """ Warning formatter
    
    Args:
        message: warning message
        category (str): level of warning
        filename (str): path for warning output
        lineno (int): Where the warning originates
    
    Returns:
        Formatted warning for logging purposes

    """
    return ' {}:{}:{}: {}\n'.format(category.__name__, filename, lineno, message)

warnings.formatwarning = format_warnings

# Constants used in BGZF format
_bgzf_magic = b"\x1f\x8b\x08\x04" # First 4 bytes of BAM file
_bgzf_header = b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00\x42\x43\x02\x00" # Ideal GZIP header
_bgzf_eof = b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00BC\x02\x00\x1b\x00\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00" # 28 null byte signature at the end of a non-truncated BAM file
_bytes_BC = b"BC" # "Payload" or Subfield Identifiers 1 & 2 of GZIP header


def _as_bytes(s):
    """ Used to ensure string is treated as bytes
    
    The output
    
    Args:
        s (str): string to convert to bytes
    
    Returns:
        byte-encoded string
    
    Example:
        >>> str(_as_bytes('Hello, World').decode()) # Duck typing to check for byte-type object 
        'Hello, World'
    
    """
    if isinstance(s, bytes):
        return s
    return bytes(s, encoding='latin_1')


# Helper compiled structures
unpack_gzip_header = struct.Struct('<4BI2BH').unpack
_gzip_header_size = struct.calcsize('<4BI2BH')

unpack_subfields = struct.Struct('<2s2H').unpack
_subfield_size = struct.calcsize('<2s2H')

unpack_gzip_integrity = struct.Struct('<2I').unpack
_integrity_size = struct.calcsize('<2I')

unpack_bgzf_metaheader = struct.Struct('<4BI2BH2BH').unpack
_metaheader_size = struct.calcsize('<4BI2BH2BH')


def _bgzf_metaheader(handle):
    """ Pull out the metadata header for a BGZF block
    
    BAM files essentially concatenated GZIP blocks put together into a cohesive file
    format. The caveat to this is that the GZIP blocks are specially formatted to contain
    metadata that indicates them as being part of a larger BAM file. Due to these specifications, 
    these blocks are identified as BGZF blocks. Listed below are those specifications. These
    specifications can be found `here <https://samtools.github.io/hts-specs/SAMv1.pdf>`_.
    
    The following GZIP fields are to have these expected values:
    
    ==================== ===========  ===========
    Field:               Label:       Exp.Value:
    ==================== ===========  ===========
    Identifier1          **ID1**      31
    Identifier2          **ID2**      139
    Compression Method   **CM**       8
    Flags                **FLG**      4
    Subfield Identifier1 **SI1**      66
    Subfield Identifier2 **SI2**      67
    Subfield Length      **SLEN**     2
    ==================== ===========  ===========
    
    Args:
        handle (:py:obj:`file`): Open BAM file object
    
    Returns:
        :py:obj:`tuple` of (:py:obj:`tuple`, :py:obj:`bytes`): the unpacked metadata and its raw bytestring
    
    Raises:
        ValueError: if the header does not match expected values
    
    .. _here: https://samtools.github.io/hts-specs/SAMv1.pdf
    
    """
    meta_raw = handle.read(_metaheader_size)
    meta = unpack_bgzf_metaheader(meta_raw)
    ID1, ID2, CM, FLG, MTIME, XFL, OS, XLEN, SI1, SI2, SLEN = meta
    
    # check the header integrity
    checks = [
        ID1 == 31,
        ID2 == 139,
        CM == 8,
        FLG == 4,
        SI1 == 66,
        SI2 == 67,
        SLEN == 2]

    if not all(checks):
        raise ValueError('Malformed BGZF block')
    
    return meta, meta_raw


def get_block(handle, offset = 0):
    r""" Pulls out entire GZIP block
    
    Used primarily for copying the header block of a BAM file. However,
    it can be used to copy any BGZF block within a BAM file that starts at
    the given offset.
    
    Note:
        Does not progress file cursor position.
    
    Args:
        handle (:py:obj:`file`): open BAM file
        offset (int): offset of BGZF block (default: 0)
    
    Returns:
        Complete BGZF block
    
    Raises:
        ValueError: if the BGZF block header is malformed
        
    Example:
        >>> with open('./bamnostic/data/example.bam','rb') as bam:
        ...     bam_header = get_block(bam)
        ...     try:
        ...         bam_header.startswith(b'\x1f\x8b\x08\x04')
        ...     except SyntaxError:
        ...         bam_header.startswith('\x1f\x8b\x08\x04')
        True
    
    """
    
    if isinstance(handle, bamnostic.core.AlignmentFile):
        handle = handle._handle
    with open(handle.name, 'rb') as header_handle:
        header_handle.seek(offset) # get to the start of the BGZF block
        
        # Capture raw bytes of metadata header
        _, meta_raw = _bgzf_metaheader(header_handle)
            
        BSIZE_raw = header_handle.read(2)
        BSIZE = struct.unpack('<H', BSIZE_raw)[0]
        
        # capture the CRC32 and ISIZE fields in addition to compressed data
        # 6 = XLEN, 19 = spec offset, 8 = CRC32 & ISIZE -> -5
        block_tail = header_handle.read(BSIZE - 5)
        return meta_raw + BSIZE_raw + block_tail


def _load_bgzf_block(handle):
    r"""Load the next BGZF block of compressed data (PRIVATE).
    
    BAM files essentially concatenated GZIP blocks put together into a cohesive file
    format. The caveat to this is that the GZIP blocks are specially formatted to contain
    metadata that indicates them as being part of a larger BAM file. Due to these specifications, 
    these blocks are identified as BGZF blocks.
    
    Args:
        handle (:py:obj:`file`): open BAM file
    
    Returns:
        deflated GZIP data
    
    Raises:
        ValueError: if CRC32 or ISIZE do not match deflated data
    
    Example:
        >>> with open('./bamnostic/data/example.bam','rb') as bam:
        ...     block = _load_bgzf_block(bam)
        ...     try:
        ...         block[0] == 53 and block[1].startswith(b'BAM\x01')
        ...     except TypeError:
        ...         block[0] == 53 and block[1].startswith('BAM\x01')
        True
    
    """
    
    # Pull in the BGZF block header information
    header, _ = _bgzf_metaheader(handle)
    XLEN = header[-4]
    BSIZE = struct.unpack('<H', handle.read(2))[0]
    
    # Expose the compressed data
    d_size = BSIZE - XLEN -19
    d_obj = zlib.decompressobj(-15)
    data = d_obj.decompress(handle.read(d_size)) + d_obj.flush()
    
    # Checking data integrity
    CRC32, ISIZE = unpack_gzip_integrity(handle.read(_integrity_size))
    deflated_crc = zlib.crc32(data)
    if deflated_crc < 0: 
        deflated_crc = deflated_crc % (1<<32)
    if CRC32 != deflated_crc:
        raise ValueError('CRCs are not equal: is {}, not {}'.format(CRC32, deflated_crc))
    if ISIZE != len(data):
        raise ValueError('unequal uncompressed data size')
    
    return BSIZE+1, data


class BAMheader(object):
    """ Parse and store the BAM file header
    
    The BAM header is the plain text and byte-encoded metadata of a given BAM file.
    Information stored in the header are the number, length, and name of the reference
    sequences that reads were aligned to; version of software used; read group identifiers; etc.
    The BAM_ format also stipulates that the first block of any BAM file should be reserved
    just for the BAM header block. 
    
    Attributes:
        _header_block (:py:obj:`bytes`): raw byte stream of header block
        _SAMheader_raw (:py:obj:`bytes`): the deflated plain text string (if present)
        _SAMheader_end (int): byte offset of the end of SAM header
        _BAMheader_end (int): byte offset of the end of the BAM header
        SAMheader (:py:obj:`dict`): parsed dictionary of the SAM header
        n_refs (int): number of references
        refs (:py:obj:`dict`): reference names and lengths listed in the BAM header
    
    .. _BAM: https://samtools.github.io/hts-specs/SAMv1.pdf
    
    """
    
    __slots__ = ['_magic', '_header_length', '_header_block', '_SAMheader_raw',
                 '_SAMheader_end', 'SAMheader', 'n_refs', 'refs', '_BAMheader_end']
    
    def __init__(self, _io):
        """ Initialize the header
        
        Args:
            _io (:py:obj:`file`): opened BAM file object
        
        Raises:
            ValueError: if BAM magic line not found at the top of the file
        
        """
        magic, self._header_length = unpack('<4si', _io)
        
        if magic != b'BAM\x01':
            raise ValueError('Incorrect BAM magic line. File head may be unaligned or this is not a BAM file')
        
        if self._header_length > 0:
            # If SAM header is present, it is in plain text. Process it and save it as rows
            self._SAMheader_raw = unpack('<{}s'.format(self._header_length), _io)
            self.SAMheader = {}
            for row in self._SAMheader_raw.decode().split('\n'):
                row = row.split('\t')
                key, fields = row[0], row[1:]
                if key.startswith('@'):
                    key = key[1:]
                    fields_dict = {}
                    for field in fields:
                        tag, value = field.split(':')
                        try:
                            value = int(value)
                        except ValueError:
                            value = value
                        fields_dict[tag] = value
                    self.SAMheader.setdefault(key, []).append(fields_dict)
        else:
            self._SAMheader_raw = None
            self.SAMheader = None
            
        self._SAMheader_end = _io._handle.tell()
        
        # Each reference is listed with the @SQ tag. We need the number of refs to process the data
        self.n_refs = unpack('<i', _io)
        
        # create a dictionary of all the references and their lengths
        self.refs = {}
        for r in range(self.n_refs):
            name_len = unpack_int32(_io.read(4))[0]
            ref_name = unpack('{}s'.format(name_len-1), _io.read(name_len)[:-1]) # get rid of null: \x00
            ref_len = unpack_int32(_io.read(4))[0]
            self.refs.update({r: (ref_name.decode(), ref_len)})
        self._BAMheader_end = _io._handle.tell()
        
        self._header_block = get_block(_io)

    def to_header(self):
        """ Allows the user to directly copy the header of another BAM file
        
        Returns:
            (bytesarray): packed byte code of entire header BGZF block 
            
        """
        
        return self._header_block
        
    def __call__(self):
        """ Used as a synonym for printing by calling the object directly
        
        Note:
            Preferentially prints out the SAM header (if present). Otherwise, it will print
            the string representation of the BAM header dictionary
            
        """
        return self._SAMheader_raw.decode().rstrip() if self._SAMheader_raw else self.refs
    
    def __repr__(self):
        return self._SAMheader_raw.decode().rstrip() if self._SAMheader_raw else str(self.refs)
    
    def __str__(self):
        """ Used for printing the header
        
        Note:
            Preferentially prints out the SAM header (if present). Otherwise, it will print
            the string representation of the BAM header dictionary
            
        """
        
        return self._SAMheader_raw.decode().rstrip() if self._SAMheader_raw else str(self.refs)
    

class BgzfReader(object):
    """ The BAM reader. Heavily modified from Peter Cock's BgzfReader.
    
    Attributes:
        header: representation of header data (if present)
        lengths (:py:obj:`list` of :py:obj:`int`): lengths of references listed in header
        nocoordinate (int): number of reads that have no coordinates
        nreferences (int): number of references in header
        ref2tid (:py:obj:`dict` of :py:obj:`str`, :py:obj:`int`): refernce names and refID dictionary
        references (:py:obj:`list` of :py:obj:`str`): names of references listed in header
        text (str): SAM header (if present)
        unmapped (int): number of unmapped reads
            
    Note:
        This implementation is likely to change. While the API was meant to 
        mirror `pysam`, it makes sense to include the `pysam`-like API in an extension
        that will wrap the core reader. This would be a major refactor, and therefore 
        will not happen any time soon (30 May 2018).
        
    """
    
    def __init__(self, filepath_or_object, mode="rb", max_cache=128, index_filename = None,
                filename = None, check_header = False, check_sq = True, reference_filename = None,
                filepath_index = None, require_index = False, duplicate_filehandle = None,
                ignore_truncation = False):
        """Initialize the class.
        
        Args:
            filepath_or_object (str | :py:obj:`file`): the path or file object of the BAM file
            mode (str): Mode for reading. BAM files are binary by nature (default: 'rb').
            max_cache (int): number of desired LRU cache size, preferably a multiple of 2 (default: 128).
            index_filename (str): path to index file (BAI) if it is named differently than the BAM file (default: None).
            filename (str | :py:obj:`file`): synonym for `filepath_or_object`
            check_header (bool): Obsolete method maintained for backwards compatibility (default: False)
            check_sq (bool): Inspect BAM file for `@SQ` entries within the header
            reference_filename (str): Not implemented. Maintained for backwards compatibility
            filepath_index (str): synonym for `index_filename`
            require_index (bool): require the presence of an index file or raise (default: False)
            duplicate_filehandle (bool): Not implemented. Raises warning if True.
            ignore_truncation (bool): Whether or not to allow trucated file processing (default: False).
        
        """
        
        # Set up the LRU buffer dictionary
        if max_cache < 1:
            raise ValueError("Use max_cache with a minimum of 1")
        self._buffers = LruDict(max_cache=max_cache)
        
        # handle contradictory arguments caused by synonyms
        if filepath_or_object and filename and filename != filepath_or_object:
            raise ValueError('filepath_or_object and filename parameters do not match. Try using only one')
        elif filepath_or_object:
            pass
        else:
            if filename:
                filepath_or_object = filename
            else:
                raise ValueError('either filepath_or_object or filename must be set')
        
        # Check to see if file object or path was passed
        if isinstance(filepath_or_object, io.IOBase):
            handle = fileobj
        else:
            handle = open(filepath_or_object, "rb")
        
        self._text = "b" not in mode.lower()
        if 'b' not in mode.lower():
            raise IOError('BAM file requires binary mode ("rb")')
        
        # Connect to the BAM file
        self._handle = handle
        
        # Check BAM file integrity
        if not ignore_truncation:
            if self._check_truncation():
                raise Exception('BAM file may be truncated. Turn off ignore_truncation if you wish to continue')
        
        # Connect and process the Index file (if present)
        self._index = None
        
        if filepath_index and index_filename and index_filename != filepath_index:
            raise IOError('Use index_filename or filepath_or_object. Not both')
        
        self._check_idx = self.check_index(index_filename if index_filename else filepath_index, require_index)
        self._init_index()
        
        # Load the first block into the buffer and intialize cursor attributes
        self._block_start_offset = None
        self._block_raw_length = None
        self._load_block(handle.tell())
        
        # Load in the BAM header as an instance attribute
        self._load_header(check_sq)

        # Helper dictionary for changing reference names to refID/TID
        self.ref2tid = {v[0]: k for k,v in self._header.refs.items()}
        
        # Final exception handling
        if check_header:
            warnings.warn('Obsolete method', UserWarning)
        if duplicate_filehandle:
            warnings.warn('duplicate_filehandle not necessary as the C API for samtools is not used', UserWarning)
        if reference_filename:
            raise NotImplementedError('CRAM file support not yet implemented')
    
    def _load_block(self, start_offset=None):
        """(PRIVATE) Used to load next BGZF block into the buffer, and orients the cursor position.
        
        Args:
            start_offset (int): byte offset of BGZF block (default: None)
        
        """
        
        if start_offset is None:
            # If the file is being read sequentially, then _handle.tell()
            # should be pointing at the start of the next block.
            # However, if seek has been used, we can't assume that.
            start_offset = self._block_start_offset + self._block_raw_length
        if start_offset == self._block_start_offset:
            self._within_block_offset = 0
            return
        elif start_offset in self._buffers:
            # Already in cache
            try:
                self._buffer, self._block_raw_length = self._buffers[start_offset]
            except TypeError:
                pass
            self._within_block_offset = 0
            self._block_start_offset = start_offset
            return
        
        # Now load the block
        handle = self._handle
        if start_offset is not None:
            handle.seek(start_offset)
        self._block_start_offset = handle.tell()
        try:
            block_size, self._buffer = _load_bgzf_block(handle)
        except StopIteration:
            # EOF
            block_size = 0
            if self._text:
                self._buffer = ""
            else:
                self._buffer = b""
        self._within_block_offset = 0
        self._block_raw_length = block_size
        
        # Finally save the block in our cache,
        self._buffers[self._block_start_offset] = self._buffer, block_size
    
    def check_index(self, index_filename = None, req_idx = False):
        """ Checks to make sure index file is available. If not, it disables random access.
        
        Args:
            index_filename (str): path to index file (BAI) if it does not fit naming convention (default: None).
            req_idx (bool): Raise error if index file is not present (default: False).
        
        Returns:
            (bool): True if index is present, else False
        
        Raises:
            IOError: If the index file is closed or index could not be opened
        
        Warns:
            UserWarning: If index could not be loaded. Random access is disabled.

        Examples:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam)
            >>> bam.check_index(bamnostic.example_bam + '.bai')
            True
            
            >>> bam.check_index('not_a_file.bai')
            False
            
        """
        if index_filename is None:
            possible_index_path = r'./{}.bai'.format(os.path.relpath(self._handle.name))
            if os.path.isfile(possible_index_path):
                self._index_path = possible_index_path
                self._random_access = True
                return True
            else:
                if req_idx:
                    raise IOError('htsfile is closed or index could not be opened')
                warnings.warn("No supplied index file and '{}' was not found. Random access disabled".format(possible_index_path), UserWarning)
                self._random_access = False
                return False
        else:
            if os.path.isfile(index_filename):
                self._index_path = index_filename
                self._random_access = True
                return True
            else:
                if req_idx:
                    raise IOError('htsfile is closed or index could not be opened')
                warnings.warn("Index file '{}' was not found. Random access disabled".format(index_filename), UserWarning)
                self._random_access = False
                return False
    
    def _init_index(self):
        """Initialize the index file (BAI)"""
        
        if self._check_idx:
            self._index = bamnostic.bai.Bai(self._index_path)
            self.nocoordinate = self._index.n_no_coor
            self.unmapped = sum(self._index.unmapped[unmapped].n_unmapped \
                                for unmapped in self._index.unmapped) + self.nocoordinate
            
    def _check_sq(self):
        """ Inspect BAM file for @SQ entries within the header
        
        The implementation of this check is for BAM files specifically. I inspects
        the SAM header (if present) for the `@SQ` entires. However, if the SAM header
        is not present, will inspect the BAM header for reference sequence entries. If this 
        test ever returns `FALSE`, the BAM file is not operational.
        
        Returns:
            (bool): True if present, else false
        
        Example:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam._check_sq()
            True
           
        """
        
        if self._header._header_length == 0:
            if not self._header.refs:
                return False
        else:
            if 'SQ' not in self._header.SAMheader:
                return False
        return True
            
    def _load_header(self, check_sq = True):
        """ Loads the header into the reader object
        
        Args:
            check_sq (bool): whether to check for file header or not (default: True).
        
        Raises:
            KeyError: If 'SQ' entry is not present in BAM header
        """
        
        self._header = BAMheader(self)
        self.header = self._header.SAMheader if self._header.SAMheader else self._header
        self.text = self._header._SAMheader_raw
        
        # make compatible with pysam attributes, even though the data exists elsewhere
        self.references = []
        self.lengths = []
        for n in range(self._header.n_refs):
            self.references.append(self._header.refs[n][0])
            self.lengths.append(self._header.refs[n][1])
        self.nreferences = self._header.n_refs
        
        if check_sq:
            if not self._check_sq():
                raise KeyError('No SQ entries in header')
    
    def _check_truncation(self):
        """ Confusing function to check for file truncation.
        
        Every BAM file should contain an EOF signature within the last
        28 bytes of the file. This function checks for that signature.
        
        Returns:
            (bool): True if truncated, else False
        
        Warns:
            BytesWarning: if no EOF signature found.
        """
        
        temp_pos = self._handle.tell()
        self._handle.seek(-28, 2)
        eof = self._handle.read()
        self._handle.seek(temp_pos)
        if eof == _bgzf_eof:
            return False
        else:
            warnings.BytesWarning('No EOF character found. File may be truncated')
            return True
    
    def has_index(self):
        """Checks if file has index and it is open
        
        Returns:
            bool: True if present and opened, else False
        """
        
        if self._check_idx and self._index:
            return self._check_idx
    
    def tell(self):
        """Return a 64-bit unsigned BGZF virtual offset."""
        
        return make_virtual_offset(self._block_start_offset, self._within_block_offset)
    
    def seek(self, virtual_offset):
        """Seek to a 64-bit unsigned BGZF virtual offset.
        
        A virtual offset is a composite number made up of the compressed
        offset (`coffset`) position of the start position of the BGZF block that
        the position originates within, and the uncompressed offset (`uoffset`) 
        within the deflated BGZF block where the position starts. The virtual offset
        is defined as
        
        `virtual_offset = coffset << 16 | uoffset`
        
        Args:
            virtual_offset (int): 64-bit unsigned composite byte offset
        
        Returns:
            virtual_offset (int): an echo of the new position
        
        Raises:
            ValueError: if within block offset is more than block size
            AssertionError: if the start position is not the block start position
        
        Example:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam.seek(10)
            10
            
            >>> bam.seek(bamnostic.utils.make_virtual_offset(0, 42))
            Traceback (most recent call last):
                ...
            ValueError: Within offset 42 but block size only 38
        
        """
        
        # Do this inline to avoid a function call,
        # start_offset, within_block = split_virtual_offset(virtual_offset)
        start_offset = virtual_offset >> 16
        within_block = virtual_offset ^ (start_offset << 16)
        if start_offset != self._block_start_offset:
            # Don't need to load the block if already there
            # (this avoids a function call since _load_block would do nothing)
            self._load_block(start_offset)
            assert start_offset == self._block_start_offset
        if within_block > len(self._buffer):
            if not (within_block == 0 and len(self._buffer) == 0):
                raise ValueError("Within offset %i but block size only %i"
                                % (within_block, len(self._buffer)))
        self._within_block_offset = within_block
        return virtual_offset
    
    def read(self, size=-1):
        """Read method for the BGZF module.
        
        Args:
            size (int): the number of bytes to read from file. Advances the cursor.
            
        Returns:
            data (:py:obj:`bytes`): byte string of length `size`

        Raises:
            NotImplementedError: if the user tries to read the whole file
            AssertionError: if read does not return any data
        
        """
        
        if size < 0:
            raise NotImplementedError("Don't be greedy, that could be massive!")
        elif size == 0:
            if self._text:
                return ""
            else:
                return b""
        elif self._within_block_offset + size <= len(self._buffer):
            # This may leave us right at the end of a block
            # (lazy loading, don't load the next block unless we have too)
            data = self._buffer[self._within_block_offset:self._within_block_offset + size]
            self._within_block_offset += size
            assert data  # Must be at least 1 byte
            return data
        else:
            # if read data overflows to next block
            # pull in rest of data in current block
            data = self._buffer[self._within_block_offset:]
            
            # decrement size so that we only pull the rest of the data
            # from next block
            size -= len(data)
            self._load_block()  # will reset offsets
            
            if not self._buffer:
                return data  # EOF
            
            # if there is still more to read
            elif size:
                # pull rest of data from next block
                return data + self.read(size)
            else:
                # Only needed the end of the last block
                return data
    
    def readline(self):
        """Read a single line for the BGZF file.
        
        Binary operations do not support `readline()`. Code is commented
        out for posterity sake
        
        """
        raise NotImplementedError("Readline does not work on byte data")
        
        # i = self._buffer.find(self._newline, self._within_block_offset)
        # # Three cases to consider,
        # if i == -1:
            # # No newline, need to read in more data
            # data = self._buffer[self._within_block_offset:]
            # self._load_block()  # will reset offsets
            # if not self._buffer:
                # return data  # EOF
            # else:
                # # TODO - Avoid recursion
                # return data + self.readline()
        # elif i + 1 == len(self._buffer):
            # # Found new line, but right at end of block (SPECIAL)
            # data = self._buffer[self._within_block_offset:]
            # # Must now load the next block to ensure tell() works
            # self._load_block()  # will reset offsets
            # assert data
            # return data
        # else:
            # # Found new line, not at end of block (easy case, no IO)
            # data = self._buffer[self._within_block_offset:i + 1]
            # self._within_block_offset = i + 1
            # # assert data.endswith(self._newline)
            # return data
    
    def fetch(self, contig = None, start = None, stop = None, region = None,
            tid = None, until_eof = False, multiple_iterators = False,
            reference = None, end = None):
        r"""Creates a generator that returns all reads within the given region
        
        Args:
            contig (str): name of reference/contig
            start (int): start position of region of interest (0-based)
            stop (int): stop position of region of interest (0-based)
            region (str): SAM region formatted string. Accepts tab-delimited values as well
            tid (int): the refID or target id of a reference/contig
            until_eof (bool): iterate until end of file
            mutiple_iterators (bool): allow multiple iterators over region. Not Implemented.
                            Notice: each iterator will open up a new view into
                                    the BAM file, so overhead will apply.
            reference (str): synonym for `contig`
            end (str): synonym for `stop`
        
        Yields:
            reads over the region of interest if any
        
        Raises:
            ValueError: if the genomic coordinates are out of range or invalid
            KeyError: Reference is not found in header
        
        Notes:
            SAM region formatted strings take on the following form:
            'chr1:100000-200000'
        
        Usage: 
                AlignmentFile.fetch(contig='chr1', start=1, stop= 1000)
                AlignmentFile.fetch('chr1', 1, 1000)
                AlignmentFile.fetch('chr1:1-1000')
                AlignmentFile.fetch('chr1', 1)
                AlignmentFile.fetch('chr1')
        
        Examples:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> next(bam.fetch('chr1', 1, 10)) # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
            EAS56_57:6:190:289:82 ... MF:C:192
            
            >>> next(bam.fetch('chr10', 1, 10))
            Traceback (most recent call last):
                ...
            KeyError: 'chr10 was not found in the file header'
            
            >>> next(bam.fetch('chr1', 1700, 1701))
            Traceback (most recent call last):
                ...
            ValueError: Genomic region out of bounds.
            
            >>> next(bam.fetch('chr1', 100, 10))
            Traceback (most recent call last):
                ...
            AssertionError: Malformed region: start should be <= stop, you entered 100, 10
        """
        
        if not self._random_access:
            raise ValueError('Random access not available due to lack of index file')
        if multiple_iterators:
            raise NotImplementedError('multiple_iterators not yet implemented')
        
        # Handle the region parsing
        if type(contig) is Roi:
            query = contig
        elif region:
            query = region_parser(region)
        else:
            if (contig and reference) and (contig != reference):
                raise ValueError('either contig or reference must be set, not both')
            
            elif reference and not contig:
                contig = reference
                
            elif tid is not None and not contig:
                contig = self.get_reference_name(tid)
            
            if contig and tid is None:
                tid = self.get_tid(contig)
            else:
                if self.ref2tid[contig] != tid:
                    raise ValueError('tid and contig name do not match')
            
            if end and not stop:
                stop = end
            else:
                if (stop and end) and (stop != end):
                    raise ValueError('either stop or end must be set, not both')
            
            if contig and not start:
                query = region_parser(contig)
            elif contig and not stop:
                query = region_parser((contig, start))
            else:
                query = region_parser((contig, start, stop))
        
        try:
            if query.start > self._header.refs[tid][1]:
                raise ValueError('Genomic region out of bounds.')
            if query.stop is None:
                # set end to length of chromosome
                stop = self._header.refs[tid][1]
            else:
                stop = query.stop
            assert query.start <= stop, 'Malformed region: start should be <= stop, you entered {}, {}'.format(query.start, stop)
        except KeyError:
            raise KeyError('{} was not found in the file header'.format(query.contig))
            
        # from the index, get the virtual offset of the chunk that
        # begins the overlapping region of interest
        first_read_block = self._index.query(tid, start, stop)
        if first_read_block is None:
            return
        # move to that virtual offset...should load the block into the cache
        # if it hasn't been visited before
        self.seek(first_read_block)
        boundary_check = True
        while boundary_check:
            next_read = next(self)
            if not until_eof:
                # check to see if the read is out of bounds of the region
                if next_read.reference_name != contig:
                    boundary_check = False
                if start < stop < next_read.pos:
                    boundary_check = False
                # check for stop iteration
                if next_read:
                    yield next_read
                else:
                    return
            else:
                try:
                    yield next_read
                except:
                    return
        else:
            return 
    
    def count(self, contig=None, start=None, stop=None, region=None, 
              until_eof=False, tid = None, read_callback='nofilter',
              reference=None, end=None):
        r"""Count the number of reads in the given region
        
        Note: this counts the number of reads that **overlap** the given region.
        
        Can potentially make use of a filter for the reads (or custom function
        that returns `True` or `False` for each read). 
        
        Args:
            contig (str): the reference name (Default: None)
            reference (str): synonym for `contig` (Default: None)
            start (int): 0-based inclusive start position (Default: None)
            stop (int): 0-based exclusive start position (Default: None)
            end (int): Synonymn for `stop` (Default: None)
            region (str): SAM-style region format. 
                        Example: 'chr1:10000-50000' (Default: None)
            until_eof (bool): count number of reads from start to end of file
                            Note, this can potentially be an expensive operation.
                            (Default: False)
            read_callback (str|function): select (or create) a filter of which
                          reads to count. Built-in filters:
                                `all`: skips reads that contain the following flags:
                                    0x4 (4): read unmapped
                                    0x100 (256): not primary alignment
                                    0x200 (512): QC Fail
                                    0x400 (1024): PCR or optical duplcate
                                `nofilter`: uses all reads (Default)
                            The user can also supply a custom function that
                            returns boolean objects for each read
        Returns:
            (int): count of reads in the given region that meet parameters
        
        Raises:
            ValueError: if genomic coordinates are out of range or invalid or random access is disabled
            RuntimeError: if `read_callback` is not properly set
            KeyError: Reference is not found in header
            AssertionError: if genomic region is malformed
        
        Notes:
            SAM region formatted strings take on the following form:
            'chr1:100000-200000'
        
        Usage: 
                AlignmentFile.count(contig='chr1', start=1, stop= 1000)
                AlignmentFile.count('chr1', 1, 1000)
                AlignmentFile.count('chr1:1-1000')
                AlignmentFile.count('chr1', 1)
                AlignmentFile.count('chr1')
        
        Example:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam.count('chr1', 1, 100)
            3
            
            >>> bam.count('chr1', 1, 100, read_callback='all')
            2
            
            >>> bam.count('chr10', 1, 10)
            Traceback (most recent call last):
                ...
            KeyError: 'chr10 was not found in the file header'
            
            >>> bam.count('chr1', 1700, 1701)
            Traceback (most recent call last):
                ...
            ValueError: Genomic region out of bounds.
            
            >>> bam.count('chr1', 100, 10)
            Traceback (most recent call last):
                ...
            AssertionError: Malformed region: start should be <= stop, you entered 100, 10
            
        """
        # pass the signature to fetch
        signature = locals()
        signature.pop('read_callback')
        signature.pop('self')
        roi_reads = self.fetch(**signature)
        # make `nofilter` the default filter unless told otherwise
        #read_callback = kwargs.get('read_callback', 'nofilter')
    
        # go through all the reads over a given region and count them
        count = 0
        for read in roi_reads:
            if read_callback == 'nofilter':
                count += 1
                
            # check the read flags against filter criteria
            elif read_callback == 'all':
                if not read.flag & 0x704: # hex for filter criteria flag bits
                    count += 1
            elif callable(read_callback):
                if read_callback(read):
                    count += 1
            else:
                raise RuntimeError('read_callback should be "all", "nofilter", or a custom function that returns a boolean')
        return count
    
    def get_index_stats(self):
        """ Inspects the index file (BAI) for alignment statistics.
        
        Every BAM index file contains metrics regarding the alignment
        process for the given BAM file. The stored data are the number
        of mapped and unmapped reads for a given reference. Unmapped reads
        are paired end reads where only one part is mapped. Additionally,
        index files also contain the number of unplaced unmapped reads. This
        is stored within the `nocoordinate` instance attribute (if present).
        
        Returns:
            idx_stats (:py:obj:`list` of :py:obj:`tuple`): list of tuples for each reference in the order seen in the header. Each tuple contains the number of mapped reads, unmapped reads, and the sum of both.
        
        Raises:
            AssertionError: if the index file is not available
        
        Example:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam.get_index_stats()
            [(1446, 18, 1464), (1789, 17, 1806)]
            
            >>> bam_no_bai = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb', index_filename='not_a_file.bai')
            >>> bam_no_bai.get_index_stats()
            Traceback (most recent call last):
                ...
            AssertionError: No index available
            
        """
        
        assert self._check_idx, 'No index available'
        idx_stats = []
        for ref in range(self._header.n_refs):
            try:
                mapped = self._index.unmapped[ref].n_mapped
                unmapped = self._index.unmapped[ref].n_unmapped
                idx_stats.append((mapped, unmapped, mapped + unmapped))
            except KeyError:
                idx_stats.append((0, 0, 0))
        return idx_stats
    
    def is_valid_tid(self, tid):
        """ Return `True` if TID/RefID is valid.
        
        Returns:
            `True` if TID/refID is valid, else `False`
        
        Example:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam.is_valid_tid(0)
            True
            
            >>> bam.is_valid_tid(10) # because there are only 2 in this file
            False
        """
        return tid in self._header.refs
    
    def get_reference_name(self, tid):
        """ Convert TID/refID to reference name.
        
        The TID/refID is the position a reference sequence is seen
        within the header file of the BAM file. The references are
        sorted by ASCII order. Therefore, for a **Homo sapien** aligned
        to GRCh38, 'chr10' comes before 'chr1' in the header. Therefore,
        'chr10' would have the TID/refID of 0, not 'chr1'.
        
        Args:
            tid (int): TID/refID of desired reference/contig
        
        Returns:
            String representation of chromosome if valid, else None
        
        Raises:
            KeyError: if TID/refID is not valid
        
        Examples:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam.get_reference_name(0)
            'chr1'
            
            >>> bam.get_reference_name(10)
            Traceback (most recent call last):
                ...
            KeyError: '10 is not a valid TID/refID for this file.'
        
        """
        if self.is_valid_tid(tid):
            return self._header.refs[tid][0]
        else:
            raise KeyError('{} is not a valid TID/refID for this file.'.format(tid))
    
    def get_tid(self, reference):
        """ Convert reference/contig name to refID/TID.
        
        The TID/refID is the position a reference sequence is seen
        within the header file of the BAM file. The references are
        sorted by ASCII order. Therefore, for a **Homo sapien** aligned
        to GRCh38, 'chr10' comes before 'chr1' in the header. Therefore,
        'chr10' would have the TID/refID of 0, not 'chr1'.
        
        Args:
            reference (str): reference/contig name
        
        Returns:
            (int): the TID/refID of desired reference/contig
        
        Raises:
            KeyError: if reference name not found file header
        
        Example:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam.get_tid('chr1')
            0
            
            >>> bam.get_tid('chr10')
            Traceback (most recent call last):
                ...
            KeyError: 'chr10 was not found in the file header'
        
        """
        
        tid = self.ref2tid.get(reference, -1)
        if tid == -1:
            raise KeyError('{} was not found in the file header'.format(reference))
        return tid
    
    def head(self, n = 5, multiple_iterators = False):
        """ List out the first **n** reads of the file.
        
        This method is primarily used when doing an initial exploration
        of the data. Whether or not `multiple_iterators` is used, cursor
        position within the file will not change.
        
        Note:
            Using `multiple_interators` opens a new file object of the 
            same file currently in use and, thus, impacts the memory
            footprint of your analysis.
        
        Args:
            n (int): number of aligned reads to print (default: 5)
            mutliple_iterators (bool): Whether to use current file object or create a new one (default: False).
        
        Returns:
            head_reads (:py:obj:`list` of :py:obj:`AlignedSegment`): list of **n** reads from the front of the BAM file
        
        Example:
            >>> bam = bamnostic.AlignmentFile(bamnostic.example_bam, 'rb')
            >>> bam.head(n=5)[0] # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
            EAS56_57:6:190:289:82	...	MF:C:192
            
            >>> bam.head(n = 5, multiple_iterators = True)[1] # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
            EAS56_57:6:190:289:82	...	H1:C:0
        
        """
        if multiple_iterators:
            head_iter = bamnostic.AlignmentFile(self._handle.name, index_filename = self._index_path)
        else:
            curr_pos = self.tell()
            # BAMheader uses byte specific positions (and not BGZF virtual offsets)
            self._handle.seek(self._header._BAMheader_end)
            self._load_block(self._handle.tell())
            head_iter = self
        
        head_reads = [next(head_iter) for read in range(n)]

        if multiple_iterators:
            # close the independent file object
            head_iter.close()
        else:
            # otherwise, just go back to old position
            self.seek(curr_pos)
            assert self.tell() == curr_pos
        return head_reads
    
    def __next__(self):
        """Return the next line (Py2 Compatibility)."""
        
        read = bamnostic.AlignedSegment(self)
        if not read:
            raise StopIteration
        return read

    def next(self):
        """Return the next line."""
        
        read = bamnostic.AlignedSegment(self)
        if not read:
            raise StopIteration
        return read

    def __iter__(self):
        """Iterate over the lines in the BGZF file."""
        
        return self

    def close(self):
        """Close BGZF file."""
        
        self._handle.close()
        self._buffer = None
        self._block_start_offset = None
        self._buffers = None

    def seekable(self):
        """Return True indicating the BGZF supports random access.
        
        Note:
            Modified from original Bio.BgzfReader: checks to see if BAM
            file has associated index file (BAI)
        
        """
        
        return self._check_idx

    def isatty(self):
        """Return True if connected to a TTY device."""
        
        return False

    def fileno(self):
        """Return integer file descriptor."""
        
        return self._handle.fileno()

    def __enter__(self):
        """Open a file operable with WITH statement."""
        
        return self

    def __exit__(self, type, value, traceback):
        """Close a file with WITH statement."""
        
        self.close()


class BgzfWriter(object):
    """Define a BGZFWriter object."""
    
    def __init__(self, filepath_or_object, mode="wb", compresslevel=6):
        """Initialize the class."""
        if isinstance(filepath_or_object, io.IOBase):
            handle = fileobj
        else:
            handle = open(filepath_or_object, "wb")
        # if fileobj:
            # assert filename is None
            # handle = fileobj
        # else:
            # if "w" not in mode.lower() and "a" not in mode.lower():
                # raise ValueError("Must use write or append mode, not %r" % mode)
            # if "a" in mode.lower():
                # handle = open(filename, "ab")
            # else:
                # handle = open(filename, "wb")
        self._text = "b" not in mode.lower()
        self._handle = handle
        self._buffer = b""
        self.compresslevel = compresslevel

    def _write_block(self, block):
        """Write provided data to file as a single BGZF compressed block (PRIVATE)."""
        # print("Saving %i bytes" % len(block))
        start_offset = self._handle.tell()
        assert len(block) <= 65536
        # Giving a negative window bits means no gzip/zlib headers,
        # -15 used in samtools
        c = zlib.compressobj(self.compresslevel,
                             zlib.DEFLATED,
                             -15,
                             zlib.DEF_MEM_LEVEL,
                             0)
        compressed = c.compress(block) + c.flush()
        del c
        assert len(compressed) < 65536, \
            "TODO - Didn't compress enough, try less data in this block"
        crc = zlib.crc32(block)
        # Should cope with a mix of Python platforms...
        if crc < 0:
            crc = struct.pack("<i", crc)
        else:
            crc = struct.pack("<I", crc)
        bsize = struct.pack("<H", len(compressed) + 25)  # includes -1
        crc = struct.pack("<I", zlib.crc32(block) & 0xffffffff)
        uncompressed_length = struct.pack("<I", len(block))
        # Fixed 16 bytes,
        # gzip magic bytes (4) mod time (4),
        # gzip flag (1), os (1), extra length which is six (2),
        # sub field which is BC (2), sub field length of two (2),
        # Variable data,
        # 2 bytes: block length as BC sub field (2)
        # X bytes: the data
        # 8 bytes: crc (4), uncompressed data length (4)
        data = _bgzf_header + bsize + compressed + crc + uncompressed_length
        self._handle.write(data)

    def write(self, data):
        """Write method for the class."""
        
        # TODO - Check bytes vs unicode
        data = _as_bytes(data)
        # block_size = 2**16 = 65536
        data_len = len(data)
        if len(self._buffer) + data_len < 65536:
            # print("Cached %r" % data)
            self._buffer += data
            return
        else:
            # print("Got %r, writing out some data..." % data)
            self._buffer += data
            while len(self._buffer) >= 65536:
                self._write_block(self._buffer[:65536])
                self._buffer = self._buffer[65536:]

    def flush(self):
        """Flush data explicitly."""
        while len(self._buffer) >= 65536:
            self._write_block(self._buffer[:65535])
            self._buffer = self._buffer[65535:]
        self._write_block(self._buffer)
        self._buffer = b""
        self._handle.flush()

    def close(self):
        """Flush data, write 28 bytes BGZF EOF marker, and close BGZF file.

        samtools will look for a magic EOF marker, just a 28 byte empty BGZF
        block, and if it is missing warns the BAM file may be truncated. In
        addition to samtools writing this block, so too does bgzip - so this
        implementation does too.
        """
        
        if self._buffer:
            self.flush()
        self._handle.write(_bgzf_eof)
        self._handle.flush()
        self._handle.close()

    def tell(self):
        """Return a BGZF 64-bit virtual offset."""
        return make_virtual_offset(self._handle.tell(), len(self._buffer))

    def seekable(self):
        """Return True indicating the BGZF supports random access."""
        # Not seekable, but we do support tell...
        return False

    def isatty(self):
        """Return True if connected to a TTY device."""
        return False

    def fileno(self):
        """Return integer file descriptor."""
        return self._handle.fileno()

    def __enter__(self):
        """Open a file operable with WITH statement."""
        return self

    def __exit__(self, type, value, traceback):
        """Close a file with WITH statement."""
        self.close()