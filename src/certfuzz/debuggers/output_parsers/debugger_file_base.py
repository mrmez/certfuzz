'''
Created on Jan 18, 2012

@organization: cert.org
'''
import hashlib
import logging
import os
import re

from certfuzz.debuggers.output_parsers.errors import DebuggerFileError, \
    UnknownDebuggerError


logger = logging.getLogger(__name__)

# for use with 'info registers' in GDB
registers = ('eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi',
             'edi', 'eip', 'cs', 'ss', 'ds', 'es', 'fs', 'gs')
registers64 = ('rax', 'rbx', 'rcx', 'rdx', 'rsi', 'rdi', 'rbp',
               'rsp', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14',
               'r15', 'rip', 'cs', 'ss', 'ds', 'es', 'fs', 'gs')

regex = {
    'bt_line_basic': re.compile(r'^#\d'),
    'bt_line': re.compile(r'^#\d+\s+(.*)$'),
    'bt_function': re.compile(r'.+in\s+(\S+)\s+\('),
    'bt_at': re.compile(r'\s+at\s+(\S+:\d+)'),
    'bt_addr': re.compile(r'(0x[0-9a-fA-F]+)\s+.+$'),
    'signal': re.compile(r'Program\sreceived\ssignal\s+([^,]+)'),
    'exit_code': re.compile(r'Program exited with code (\d+)'),
    'faddr': re.compile(r'^si_addr.+(0x[0-9a-zA-Z]+)'),
    'bt_line_from': re.compile(r'\bfrom\b'),
    'bt_line_at': re.compile(r'\bat\b'),
    'register': re.compile(r'(0x[0-9a-zA-Z]+)\s+(.+)$'),
    'libc_location': re.compile(r'(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+0x[0-9a-fA-F]+\s+0(x0)?\s+.+/libc[-.]'),
    'libgcc_location': re.compile(r'(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+0x[0-9a-fA-F]+\s+0(x0)?\s+.+/libgcc(_s)?[-.]'),
    'mapped_frame': re.compile(r'(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+0x[0-9a-fA-F]+\s+0(x0)?\s+(/.+)'),
    'gdb_bt_threads': re.compile(r'^\[New Thread.+'),
    'konqi_bt_threads': re.compile(r'^\[Current thread is \d+\s\(Thread\s([0-9a-zA-Z]+).+\]$'),
    'detect_konqi': re.compile(r'-- Backtrace:'),
    'detect_abrt': re.compile(r'Core was generated by'),
    'detect_gdb': re.compile(r'#\d+\s+'),
    'exploitability': re.compile(r'^Exploitability Classification: (.+)$')
}

# There are a number of functions that are typically found in crash backtraces,
# yet are side effects of a crash and are not directly relevant to identifying
# the uniqueness of the crash. So we explicitly blacklist them so they won't be
# used in determining the crash backtrace hash.
blacklist = ('__kernel_vsyscall', 'abort', 'raise', 'malloc', 'free',
             '*__GI_abort', '*__GI_raise', 'malloc_printerr', '__libc_message',
             'malloc_consolidate', '_int_malloc', '__libc_calloc',
             '_dl_new_object', '_dl_map_object_from_fd', '_dl_catch_error',
             '_dl_open', 'do_dlopen', 'dlerror_run', '*__GI___libc_dlopen_mode',
             '_dl_map_object', 'dl_open_worker', 'munmap_chunk', '*__GI___backtrace',
             '_dl_addr_inside_object', '_int_free', '*__GI___libc_free',
             '__malloc_assert', 'sYSMALLOc', '_int_realloc', '*__GI___libc_malloc',
             '*__GI___libc_realloc', '_int_memalign', '*__GI___libc_memalign',
             '__posix_memalign', 'malloc_consolidate', '__libc_malloc', '__libc_realloc',
             'g_assertion_message', 'g_assertion_message_expr',
             )


def check_thread_type(line):
    if regex['detect_konqi'].match(line):
        return 'konqi'
    elif regex['detect_abrt'].match(line):
        return 'abrt'
    elif regex['detect_gdb'].match(line):
        return 'gdb'
    else:
        return False


def detect_format(debugger_output_file):
    logger.debug('Checking format of %s', debugger_output_file)
    with open(debugger_output_file, 'r') as f:
        for line in f.readlines():
            thread_format = check_thread_type(line.strip())
            if thread_format:
                return thread_format

    # if you got here it's because you couldn't figure out what kind of file
    # you're dealing with
    raise UnknownDebuggerError(
        'Unrecognized debugger for %s' % debugger_output_file)


class DebuggerFile(object):
    '''
    classdocs
    '''

    def __init__(self, path, exclude_unmapped_frames=True, keep_uniq_faddr=False):
        '''
        Create a GDB file object from the gdb output file <file>
        @param lines: The lines of the gdb file
        @param is_crash: True if gdb file represents a testcase
        @param is_assert_fail: True if gdb file represents an assert_fail
        @param is_debugbuild: True if gdb file contains source code lines
        '''
        logger.debug('initializing %s', path)
        self.file = path
        self.debugger_output = None
        self.lines = []

        self.exclude_unmapped_frames = exclude_unmapped_frames

        # collect data about the gdb output
        self.backtrace = []
        self.backtrace_without_questionmarks = []
        self.registers = {}
        # make a copy of registers list we're looking for
        self.registers_sought = list(registers)
        self.registers_hex = {}
        self.hashable_backtrace = []
        self.hashable_backtrace_string = ''
        self.module_map = []
        self.exit_code = None
        self.signal = None
        self.is_corrupt_stack = False
        self.is_crash = True
        self.is_assert_fail = False
        self.is_debugbuild = False
        self.libc_start_addr = 0
        self.libc_end_addr = 0
        self.libgcc_start_addr = 0
        self.libgcc_end_addr = 0
        self.used_pc = False
        self.debugger_missed_stack_corruption = False
        self.total_stack_corruption = False
        self.pc_in_function = False
        self.is_64bit = False
        self.pc_name = 'eip'
        self.keep_uniq_faddr = keep_uniq_faddr
        self.faddr = None
        self.exp = 'UNKNOWN'

        # a list of functions to be called on each line
        # if line_callbacks is set in child classes these ones
        # will not be added
        if not hasattr(self, 'line_callbacks'):
            self.line_callbacks = [
                self._look_for_64bit,
                self._look_for_exit_code,
                self._look_for_debug_build,
                self._look_for_corrupt_stack,
                self._look_for_libc_location,
                self._look_for_libgcc_location,
                self._look_for_signal,
                self._look_for_crash,
                self._look_for_registers,
                self._look_for_faddr,
                self._build_module_map,
                self._look_for_exploitability,
            ]
        self._read_file()
        self._process_file()

    def _process_file(self):
        self._process_lines()
        self._process_backtrace()
        self._hashable_backtrace()

    def _hashable_backtrace(self):
        logger.debug('_hashable_backtrace')
        hashable = []

        if not self.hashable_backtrace:
            for bt in self.backtrace:
                logger.debug('bt=%s', bt)
                frame_address = 0
                bt_frame = None

                # Get the address of the current backtrace frame
                n = re.match(regex['bt_addr'], bt)
                if n:
                    # Get the frame address from the backtrace line
                    bt_frame = n.group(1)
                    frame_address = int(bt_frame, 16)

                elif self.registers_hex.get(self.pc_name) and not self.used_pc:
                    # Backtrace entry #0 doesn't have an address listed, so use EIP instead
                    # But set a flag not to use EIP again, as inline frames
                    # behave the same way
                    self.used_pc = True
                    frame_address = int(self.registers_hex[self.pc_name], 16)

                if self.libc_start_addr < frame_address < self.libc_end_addr:
                    # Don't include any backtrace frames that are in libc
                    continue

                if self.libgcc_start_addr < frame_address < self.libgcc_end_addr:
                    # Don't include any backtrace frames that are in libgcc
                    continue

                # skip blacklisted functions
                x = re.match(regex['bt_function'], bt)
                if x and x.group(1) in blacklist:
                    continue

                # If debug symbols are available, the backtrace will include
                # the line number
                m = re.search(regex['bt_at'], bt)
                if m:
                    bt_frame = m.group(1)

                    # skip anything in /sysdeps/ since they're
                    # typically part of the post-crash
                    if '/sysdeps/' in bt_frame:
                        logger.debug('Found sysdeps, skipping')
                        continue

                # Append either the frame address or source code line number
                if bt_frame:
                    hashable.append(bt_frame)

            if not hashable:
                # try a few more things to get something to hash

                if self.total_stack_corruption:
                    hashable.append('total_stack_corruption')

                if self.exit_code:
                    hashable.append('exit_code:%s' % self.exit_code)
                elif len(self.backtrace) and self.backtrace[0]:
                    # if we got here, it's because
                    # (a) there were no usable backtrace lines, AND
                    # (b) there was no exit code
                    # so we'll use whatever value was in the first line
                    # even if it would have been otherwise discarded
                    hashable.append(self.backtrace[0])

            if not hashable:
                # we've tried, but we have nothing at all to hash, then
                # even the first bt line must have been empty
                # so this can't be a crash
                self.is_crash = False

            self.hashable_backtrace = hashable
            logger.debug("hashable_backtrace: %s", self.hashable_backtrace)

        return self.hashable_backtrace

    def _hashable_backtrace_string(self, level):
        self.hashable_backtrace_string = ' '.join(
            self.hashable_backtrace[:level]).strip()
        if self.keep_uniq_faddr:
            try:
                self.hashable_backtrace_string = self.hashable_backtrace_string + \
                    ' ' + self.faddr
            except:
                logger.debug('Cannot use PC in hash')
        logger.warning(
            '_hashable_backtrace_string: %s', self.hashable_backtrace_string)
        return self.hashable_backtrace_string

    def _backtrace_without_questionmarks(self):
        logger.debug('_backtrace_without_questionmarks')
        if not self.backtrace_without_questionmarks:
            self.backtrace_without_questionmarks = [
                bt for bt in self.backtrace if not '??' in bt]
        return self.backtrace_without_questionmarks

    def backtrace_line(self, idx, l):
        m = re.match(regex['bt_line'], l)
        if m:
            item = m.group(1)
            # sometimes gdb splits across lines
            # so get the next one if it looks like '<anything> at <foo>' or
            # '<anything> from <foo>'
            next_idx = idx + 1
            while next_idx < len(self.lines):
                nextline = self.lines[next_idx]
                if re.match(regex['bt_line_basic'], nextline):
                    break
                elif re.search(regex['bt_line_from'], nextline) or re.search(regex['bt_line_at'], nextline):
                    if not "Quit anyway" in nextline and not " = " in nextline:
                        item = ' '.join((item, nextline))
                next_idx += 1

            self.backtrace.append(item)
            logger.debug('Appending to backtrace: %s', item)

    def _read_file(self):
        '''
        Reads the debugger file into memory
        '''
        logger.debug('_read_file')
        try:
            with open(self.file, 'r') as f:
                self.debugger_output = f.read()
                self.lines = [l.strip()
                              for l in self.debugger_output.splitlines()]
        except IOError, e:
            raise DebuggerFileError(e)
        except MemoryError, e:
            raise DebuggerFileError(e)

    def _process_backtrace(self):
        if not len(self.backtrace):
            logger.debug('Backtrace is empty')
            return

        # post-process the backtrace
        if self.is_corrupt_stack:
            # if we found that the stack was corrupt,
            # we can no longer trust the last backtrace line
            # so remove it
            removed_bt_line = self.backtrace.pop()
            logger.debug(
                "GDB detected corrupt stack. Removing backtrace line: %s", removed_bt_line)
        else:
            # if the last line of the backtrace is unmapped, we're in
            # corrupt stack land
            self._look_for_debugger_missed_stack_corruption()

        if self.exclude_unmapped_frames:
            self._remove_unmapped_frames()
        self._look_for_assert_fail()
        self._check_pc_in_function()

    def _process_lines(self):
        logger.debug('_process_lines')

        for idx, line in enumerate(self.lines):
            self.backtrace_line(idx, line)

            for callback in self.line_callbacks:
                # callbacks take a line as their argument
                callback(line)

    def _look_for_corrupt_stack(self, line):
        if self.is_corrupt_stack:
            return

        if 'corrupt stack' in line:
            logger.debug('Corrupt stack')
            self.is_corrupt_stack = True

    def _is_mapped_frame(self, frame_address):
        '''
        Returns true if frame_address lies within a mapped frame
        @param frame_address:
        '''
        logger.debug('_is_mapped_frame? %s', frame_address)
        if len(self.module_map):
            for module in self.module_map:
                logger.debug('Module: %s %s', module['start'], module['end'])
                if module['start'] < frame_address < module['end']:
                    logger.debug('Found address %x in module: %s' %
                                 (frame_address, module['objfile']))
                    return True
            # if you got here, it's not mapped
            return False
        else:
            # if we don't have a module map, we can't tell, so assume true
            return True

    def _get_frame_address(self, bt_line):
        n = re.match(regex['bt_addr'], bt_line)
        if n:
            # Get the frame address from the backtrace line
            frame_address = int(n.group(1), 16)
            return frame_address
        else:
            return None

    def _look_for_debugger_missed_stack_corruption(self):
        start_bt_length = len(self.backtrace)
        while self.backtrace:
            # If the outermost backtrace frame isn't from a loaded module,
            # then we're likely dealing with stack corruption
            mapped_frame = False

            frame_address = self._get_frame_address(self.backtrace[-1])
            if frame_address:
                mapped_frame = self._is_mapped_frame(frame_address)
                if not mapped_frame:
                    self.debugger_missed_stack_corruption = True
                    # we can't use this line in a backtrace, so pop it
                    removed_bt_line = self.backtrace.pop()
                    logger.debug(
                        "GDB missed corrupt stack detection. Removing backtrace line: %s", removed_bt_line)
                else:
                    # as soon as we hit a line that is a mapped
                    # frame, we're done trimming the backtrace
                    break
            else:
                # if the outermost frame of the backtrace doesn't list a memory address,
                # it's likely main(), which is fine.
                break

        end_bt_length = len(self.backtrace)

        if start_bt_length and not end_bt_length:
            # Destroyed ALL the backtrace!
            self.total_stack_corruption = True
            logger.debug('Total stack corruption. No backtrace lines left.')

    def _look_for_exit_code(self, line):
        #        if self.exit_code: return

        m = re.match(regex['exit_code'], line)
        if m:
            self.exit_code = m.group(1)
            logger.debug('Exit code: %s', self.exit_code)
            self.line_callbacks.remove(self._look_for_exit_code)

    def _look_for_signal(self, line):
        if self.signal:
            return

        m = re.match(regex['signal'], line)
        if m:
            self.signal = m.group(1)
            logger.debug('Signal: %s', self.signal)
            if self.signal == 'SIGABRT':
                # If we have a SIGABRT, the gdb-reported faulting address isn't
                # accurate.
                self.faddr = '0'

    def _look_for_faddr(self, line):
        if self.faddr:
            return

        m = re.match(regex['faddr'], line)
        if m:
            self.faddr = m.group(1)
            logger.debug('Faulting address: %s', self.faddr)

    def _look_for_exploitability(self, line):
        if self.faddr:
            return

        m = re.match(regex['exploitability'], line)
        if m:
            self.exp = m.group(1)
            logger.debug('Exploitability: %s', self.exp)

    def _look_for_crash(self, line):
        if not self.is_crash:
            return

#        logger.debug('_look_for_crash')
        if 'SIGKILL' in line:
            self.is_crash = False
        elif 'SIGHUP' in line:
            self.is_crash = False
        elif 'SIGXFSZ' in line:
            self.is_crash = False
        elif 'Program exited normally' in line:
            self.is_crash = False

    def _look_for_assert_fail(self):
        for bt in self.backtrace:
            if '__assert_fail' in bt:
                logger.debug('Assert fail')
                self.is_assert_fail = True
                break

    def _remove_unmapped_frames(self):
        for i in xrange(len(self.backtrace) - 1, -1, -1):
            bt = self.backtrace[i]
            mapped_frame = False
            frame_address = self._get_frame_address(bt)
            if frame_address is not None:
                mapped_frame = self._is_mapped_frame(frame_address)
                if not mapped_frame:
                    logger.debug(
                        'Removing unmapped backtrace frame address: %s' % bt)
                    del self.backtrace[i]
        if not len(self.backtrace):
            # No frame in the backtrace is in a mapped module
            self.total_stack_corruption = True

    def _check_pc_in_function(self):
        '''
        Look to see if the crash is in a function recognized by gdb
        If it is, we can use just 'disass' and let gdb determine how much to return
        Otherwise, we fall back to fixed offsets from $pc
        '''
        if self.backtrace:
            if not 'in ??' in self.backtrace[0]:
                self.pc_in_function = True

    def _look_for_debug_build(self, line):
        if self.is_debugbuild:
            return

        if ' at ' in line:
            logger.debug('Debug build = True')
            self.is_debugbuild = True

    def _look_for_64bit(self, line):
        '''
        Check for 64-bit process by looking at address of bt frame addresses
        '''
        if self.is_64bit:
            return
        m = re.match(regex['bt_addr'], line)
        if m:
            start_addr = m.group(1)
            logger.debug('%s length: %s', start_addr, len(start_addr))
            if len(start_addr) > 10:
                self.is_64bit = True
                logger.debug('Target process is 64-bit')
                self.pc_name = 'rip'
                self.registers_sought = list(registers64)

    def _look_for_libc_location(self, line):
        '''
        Get start and end address of libc library, for blacklisting purposes
        '''
        if self.libc_start_addr:
            return

        m = re.match(regex['libc_location'], line)
        if m:
            self.libc_start_addr = int(m.group(1), 16)
            self.libc_end_addr = int(m.group(2), 16)

    def _look_for_libgcc_location(self, line):
        '''
        Get start and end address of libc library, for blacklisting purposes
        '''
        if self.libgcc_start_addr:
            return

        m = re.match(regex['libgcc_location'], line)
        if m:
            self.libgcc_start_addr = int(m.group(1), 16)
            self.libgcc_end_addr = int(m.group(2), 16)

    def _build_module_map(self, line):
        '''
        Build list of dictionaries that contain start and end addresses for mapped modules
        '''
        m = re.match(regex['mapped_frame'], line)
        if m:
            module = {'start': int(m.group(1), 16),
                      'end': int(m.group(2), 16),
                      'objfile': m.group(4)
                      }
            self.module_map.append(module)

    def _look_for_registers(self, line):
        # short-circuit if we're out of registers to look for
        if not len(self.registers_sought):
            return

        parts = line.split()

        # short-circuit if line doesn't split
        if not len(parts):
            return
        # short-circuit if the first thing in the line isn't a register
        if not parts[0] in self.registers_sought:
            return

        r = parts[0]
        r_str = ' '.join(parts[1:])
        m = re.match(regex['register'], r_str)

        # short-circuit when no match
        if not m:
            logger.debug('Register not matched: %s', r_str)
            return

        self.registers_hex[r] = m.group(1)
        self.registers[r] = m.group(2)
        # once we've found the register, we don't have to look for it anymore
        self.registers_sought.remove(r)
        logger.debug('Register %s=%s', r, self.registers_hex[r])

    def get_testcase_signature(self, backtrace_level):
        '''
        Determines if a crash is unique. Depending on <backtrace_level>,
        it may look at a number of source code lines in the gdb backtrace, or simply
        just the memory location of the crash.
        '''
        logger.debug('get_testcase_signature')
        backtrace_string = self._hashable_backtrace_string(backtrace_level)

        if bool(backtrace_string):
            return hashlib.md5(backtrace_string).hexdigest()
        else:
            return None


def _detect_and_generate(debugger_file):
    import konqifile
    import abrtfile
    import gdbfile

    BacktraceClass = {
        'gdb': gdbfile.GDBfile,
        'abrt': abrtfile.ABRTfile,
        'konqi': konqifile.Konqifile,
    }
    try:
        bt_type = detect_format(debugger_file)
    except UnknownDebuggerError:
        logger.info("Skipping %s -- unknown type", debugger_file)
        return None

    try:
        bt = BacktraceClass[bt_type](debugger_file)
    except KeyError:
        logger.warning(
            "No class defined for type %s (%s)", bt_type, debugger_file)
        return None

    return bt


def _print_line(sig, filepath, bthash, include_bt=False):
    format_string = '%-32s\t%s'
    print format_string % (sig, filepath)
    if include_bt:
        print format_string % ('', bthash)
        print


def _analyze_file(filepath, include_bt=False):
    '''
    Gets the crash signature for the file located at <filepath>
    @param filepath: the full path to the file to analyze
    '''
    logger.debug('File: %s', filepath)
    bt = _detect_and_generate(filepath)

    sig = 'unknown_type'
    bthash = 'no_backtrace_available'

    if bt:
        sig = bt.get_testcase_signature(5)
        if bt.total_stack_corruption:
            sig = "total_stack_corruption"
        if not sig:
            sig = "no_signature_found"
        if include_bt:
            bthash = bt._hashable_backtrace_string(5)

    _print_line(sig, filepath, bthash, include_bt)

if __name__ == '__main__':
    from optparse import OptionParser

    # override the module loger with the root logger
    logger = logging.getLogger()

    hdlr = logging.StreamHandler()
    logger.addHandler(hdlr)

    usage = 'Usage: %prog [options] <directory-or-filename>'

    parser = OptionParser(usage)
    parser.add_option('', '--debug', dest='debug', action='store_true',
                      help='Enable debug messages (overrides --verbose)')
    parser.add_option('', '--verbose', dest='verbose',
                      action='store_true', help='Enable verbose messages')
    parser.add_option('', '--print-hashable-backtrace', dest='print_backtrace', action='store_true',
                      default=False, help='Prints the hashable backtrace string on a second line')
    parser.add_option(
        '', '--pattern', dest='pattern', help='File glob pattern (optional)')
    (options, args) = parser.parse_args()

    logger.setLevel(logging.WARNING)
    if options.debug:
        logger.setLevel(logging.DEBUG)
    elif options.verbose:
        logger.setLevel(logging.INFO)

    for f in args:
        if os.path.isdir(f):
            import certfuzz.fuzztools.filetools
            if options.pattern:
                pat = options.pattern
            else:
                pat = '*'

            for x in certfuzz.fuzztools.filetools.all_files(f, patterns=pat):
                # skip .svn in path
                if '.svn' in os.path.dirname(x):
                    continue
                _analyze_file(x, options.print_backtrace)
        elif os.path.isfile(f):
            _analyze_file(f, options.print_backtrace)
