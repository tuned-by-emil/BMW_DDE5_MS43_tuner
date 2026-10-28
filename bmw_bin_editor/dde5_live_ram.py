"""Connection-scoped live calibration, unlocked by an actual reversible test.

This client owns one transparent K-line connection. It never imports a saved
qualification, reconnects automatically, or programs flash. Exact readback uses
the native mapped one-byte checksum routine and can be slow at 9600 baud.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import hashlib
import time

from .dde5_ram_calibration import (BOUNDARIES, DIRECTORY_ADDRESS, EXTERNAL_BASE,
    STOCK_POOL_BYTES, _require_manager, _span_blocks, build_ram_plan, run_ram_bench,
    validate_hardware)
from .serial_transport import SerialCancelled, SerialCommunicationError, validate_port


def editable_spans(definition, *, include_axes=True):
    """Return merged, directory-bounded file spans for this table and its axes."""
    if definition is None or getattr(definition, 'discovery_unverified', True):
        raise ValueError('Select a verified 361830 table.')
    if getattr(definition, 'profile_id', '') not in ('', '361830'):
        raise ValueError('Live RAM editing requires a 361830 table.')
    if type(include_axes) is not bool:
        raise ValueError('Choose whether to include editable axes.')
    width = definition.z_width
    if width not in (1, 2, 4) or type(definition.size) is not int or definition.size <= 0:
        raise ValueError('The table must use contiguous 8-, 16- or 32-bit values.')
    spans = [(definition.z_address, definition.size * width)]
    if include_axes:
        for axis in (getattr(definition, 'x_axis', None), getattr(definition, 'y_axis', None)):
            if axis is not None and axis.address is not None:
                if (axis.element_bits not in (8, 16, 32) or type(axis.count) is not int
                        or axis.count <= 0):
                    raise ValueError('An editable axis has unsupported storage.')
                spans.append((axis.address, axis.count * (axis.element_bits // 8)))
    merged = []
    for start, count in sorted(spans):
        if type(start) is not int or type(count) is not int:
            raise ValueError('The table or axis has an invalid address or length.')
        _span_blocks(0x800000 + start, count)
        end = start + count
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return [dict(file_offset=start, bytes=end-start, logical_address=start+0x800000)
            for start, end in merged]


class LiveRamController:
    """Call all methods on the same worker thread; reports do not confer access."""

    def __init__(self, port, original, hardware, *, echo=True, cancelled=None,
                 progress=None, serial_factory=None, clock=time.monotonic, sleep=time.sleep):
        self.port = validate_port(port)
        if not isinstance(original, bytes) or len(original) != 0x100000:
            raise ValueError('Live RAM editing requires the original full 1 MiB FLS BIN.')
        self.original = original
        self.hardware = validate_hardware(hardware)
        if type(echo) is not bool:
            raise ValueError('Choose the actual cable echo behavior.')
        self.echo, self.cancelled, self.progress = echo, cancelled or (lambda: False), progress
        self.serial_factory, self.clock, self.sleep = serial_factory, clock, sleep
        self._session = None
        self._qualified = False
        self._allocated = set()
        self._cache = {}
        self._external_cursor = EXTERNAL_BASE
        self._internal_cursor = 0x3F9FD4
        self._page = 'reference'
        self._report = None
        self._terminal_report = None
        self._qualification_attempted = False
        self._limit = EXTERNAL_BASE + min(self.hardware['capacity_bytes'], STOCK_POOL_BYTES)

    @property
    def qualified(self):
        s = self._session
        return bool(self._qualified and s is not None and s.connection is not None
                    and s.connection.is_open and not s.faulted)

    def _new_report(self, operation):
        report = dict(format='BMWBinEditor-DDE5-live-RAM-v1', operation=operation, port=self.port,
            status='failed', error=None, steps=[], frames=[], details={},
            restore=dict(reference_page='not_needed', write='not_needed', session='86_left_active',
                         power_cycle_required=False, restoration_required=False))
        self._report = report
        if self._session is not None:
            self._session.report = report
        return report

    def _step(self, text):
        self._report['steps'].append(text)
        if self.progress:
            try:
                self.progress(text)
            except Exception:
                pass

    def _details(self, report):
        report['details'].update(qualified=self.qualified, page=self._page,
            external_bytes_remaining=max(0, self._limit-self._external_cursor),
            internal_bytes_remaining=max(0, 0x3FA178-self._internal_cursor),
            allocated_block_indices=sorted(self._allocated),
            exact_readback='native mapped one-byte checksums; sequential, not an atomic snapshot',
            capacity_limit='host-enforced; stock ECU allocator remains 60 KiB')
        return report

    def qualify(self, definition):
        """Run the real changed-byte test and retain only its successful link."""
        if self._qualification_attempted:
            report = self._new_report('qualify')
            report['error'] = 'Start a new controller and verify a fresh ECU boot before another qualification.'
            self._shutdown(report)
            return self._details(report)
        self._qualification_attempted = True
        candidate = []
        try:
            plan = build_ram_plan(self.original, deepcopy(definition),
                                  capacity_bytes=self.hardware['capacity_bytes'])
            report = run_ram_bench(self.port, plan=plan, hardware=self.hardware,
                probe_change=True, echo=self.echo, cancelled=self.cancelled, progress=self.progress,
                serial_factory=self.serial_factory, clock=self.clock, sleep=self.sleep,
                _handoff=lambda session, proof: candidate.append(session))
            self._report = report
            if (report['status'] != 'passed' or len(candidate) != 1
                    or report['restore']['write'] != 'verified'
                    or report['restore']['reference_page'] != 'verified'
                    or report['details'].get('changed_probe', {}).get('exact_readback_verified') is not True):
                if report['restore']['reference_page'] in ('unknown', 'required'):
                    self._page = 'unknown'
                if report['status'] == 'passed':
                    report.update(status='failed', error='The complete retained-connection qualification proof is missing.')
                self._details(report)
                self._terminal_report = deepcopy(report)
                return report
            session = candidate[0]
            if session.connection is None or not session.connection.is_open or session.faulted:
                raise SerialCommunicationError('The qualified connection is no longer open and healthy.')
            self._session = session
            state = report['details']['copied']
            self._external_cursor, self._internal_cursor = state['external_allocator_cursor'], state['internal_allocator_cursor']
            self._allocated = {b['index'] for b in plan['blocks']}
            self._cache = {b['index']: bytearray(self.original[b['logical_start']-0x800000:
                          b['logical_end_exclusive']-0x800000]) for b in plan['blocks']}
            self._page = 'reference'
            self._qualified = True
            report['details']['live_controls_unlocked'] = True
            report['operation'] = 'qualify'
            report['details']['connection_scope'] = 'this open connection only; no saved report can unlock another link'
            return self._details(report)
        except Exception as error:
            report = self._new_report('qualify')
            report.update(error=str(error), status='cancelled' if isinstance(error, SerialCancelled) else 'failed')
            if candidate and self._session is None:
                self._session = candidate[0]
            self._shutdown(report)
            return self._details(report)
        finally:
            if not self._qualified:
                for session in candidate:
                    if session.connection is not None:
                        session.close()

    def _checkpoint(self):
        if self.cancelled():
            raise SerialCancelled('Live RAM operation cancelled; restoring the reference page and closing the link.')

    def _state(self):
        state = self._session.state()
        offset = EXTERNAL_BASE-DIRECTORY_ADDRESS if self._page == 'working' else 0
        _require_manager(state, ready=True, cursor=self._external_cursor,
                         internal_cursor=self._internal_cursor, page=offset)
        return state

    def _select(self, page):
        if page not in ('reference', 'working'):
            raise ValueError('Choose reference or working calibration page.')
        self._state()
        if page != self._page:
            self._session._exchange(b'\x80\x90' + bytes([page == 'working']))
            self._page = page
            self._state()

    def _shutdown(self, report):
        self._qualified = False
        session = self._session
        if session is None:
            if self._terminal_report is not None:
                terminal = self._terminal_report
                report['restore'] = deepcopy(terminal['restore'])
                report['details'].update(deepcopy(terminal.get('details', {})))
                report['details']['terminal_operation'] = terminal.get('operation', 'qualify')
                uncertain = (terminal['restore'].get('power_cycle_required')
                    or terminal['restore'].get('restoration_required')
                    or terminal['restore'].get('reference_page') in ('required', 'unknown')
                    or terminal['restore'].get('write') in ('required', 'unknown')
                    or terminal.get('details', {}).get('port_closed') is False)
                report['details'].update(previous_operation_status=terminal['status'],
                                         previous_operation_error=terminal.get('error'))
                if uncertain:
                    report['status'] = 'failed'
                    report['error'] = terminal.get('error') or report.get('error')
            else:
                report['restore']['session'] = 'unchanged'
                report['details']['port_closed'] = True
            return
        session.report = report
        report['restore']['reference_page'] = 'required'
        if not session.faulted:
            try:
                state = session.state()
                if (state['execution_mode'] != 5 or state['transport_state'] != 0
                        or state['directory_ready_and_transaction'] & 0xFF):
                    raise SerialCommunicationError('Manager state is not idle for reference-page restoration.')
                if state['application_page_offset']:
                    session._exchange(b'\x80\x90\x00')
                    state = session.state()
                if state['application_page_offset'] != 0 or state['directory_ready_and_transaction'] & 0xFF:
                    raise SerialCommunicationError('Reference-page restoration was not verified.')
                self._page = 'reference'
                report['restore']['reference_page'] = 'verified'
            except Exception as error:
                report['details']['reference_cleanup_error'] = str(error)
        if report['restore']['reference_page'] != 'verified':
            self._page = 'unknown'
            report['restore'].update(reference_page='unknown', power_cycle_required=True, restoration_required=True)
        try:
            session.close()
        except Exception as error:
            report['details']['port_cleanup_error'] = str(error)
            report['status'] = 'failed'
            report['error'] = report['error'] or str(error)
        finally:
            self._session = None
            # Automatic/final repeated disconnects must not erase uncertainty
            # about an earlier write, page restoration, or port-close failure.
            self._terminal_report = deepcopy(report)

    def _run(self, operation, function):
        report = self._new_report(operation)
        if not self.qualified:
            report['error'] = 'Live controls are locked. This connection must pass the complete RAM write/readback qualification.'
            self._shutdown(report)
            return self._details(report)
        try:
            self._checkpoint()
            self._state()
            function(report)
            report['status'] = 'passed'
        except Exception as error:
            report.update(status='cancelled' if isinstance(error, SerialCancelled) else 'failed', error=str(error))
            self._shutdown(report)
        return self._details(report)

    def _spans(self, definition, include_axes):
        return editable_spans(deepcopy(definition), include_axes=include_axes)

    def _register(self, definition, include_axes):
        spans = self._spans(definition, include_axes)
        desired = {}
        for span in spans:
            for block in _span_blocks(span['logical_address'], span['bytes']):
                desired[block['index']] = block
        missing = [desired[i] for i in sorted(desired) if i not in self._allocated]
        # Budget every complete allocation before the first mutating request.
        expected_internal, expected_external = self._internal_cursor, self._external_cursor
        for block in missing:
            if expected_internal + block['bytes'] <= 0x3FA178:
                expected_internal += block['bytes']
            else:
                expected_external += block['bytes']
            if expected_external > self._limit:
                raise ValueError('The complete table/axis blocks exceed the verified remaining SRAM capacity.')
        session = self._session
        all_blocks = {b['index']: b for b in session.plan['blocks']}
        all_blocks.update(desired)
        session.plan['blocks'] = [all_blocks[i] for i in sorted(all_blocks)]
        session.checksum_ranges.update((b['logical_start'], b['logical_end_exclusive']-1) for b in missing)
        for block in missing:
            actual = session.read_span(block['logical_start'], block['bytes'])
            baseline = self.original[block['logical_start']-0x800000:block['logical_end_exclusive']-0x800000]
            if actual != baseline:
                raise SerialCommunicationError('A new block differs from the original BIN; no copy was requested for it.')
        for block in missing:
            self._checkpoint()
            self._state()
            target = next(max(span['logical_address'], block['logical_start']) for span in spans
                          if span['logical_address'] < block['logical_end_exclusive']
                          and span['logical_address']+span['bytes'] > block['logical_start'])
            value = self.original[target-0x800000:target-0x800000+1]
            session.write_values[target] = {value}
            session.write_byte(target, value)
            if self._internal_cursor + block['bytes'] <= 0x3FA178:
                self._internal_cursor += block['bytes']
            else:
                self._external_cursor += block['bytes']
            self._state()
            self._allocated.add(block['index'])
            self._cache[block['index']] = bytearray(self.original[block['logical_start']-0x800000:
                                                       block['logical_end_exclusive']-0x800000])
        previous_page = self._page
        if missing:
            self._select('working')
            for block in missing:
                expected = sum(self._cache[block['index']]) & 0xFFFF
                if session.checksum(block['logical_start'], block['logical_end_exclusive']-1, expected) != expected:
                    raise SerialCommunicationError('A newly copied working block failed its 16-bit byte-sum check.')
            self._select(previous_page)
        self._checkpoint()
        self._state()
        return spans

    def _cached(self, address, count):
        result = bytearray()
        for target in range(address, address+count):
            index = bisect_right(BOUNDARIES, target)-1
            result.append(self._cache[index][target-BOUNDARIES[index]])
        return bytes(result)

    def _cache_put(self, address, data):
        for target, byte in enumerate(data, address):
            index = bisect_right(BOUNDARIES, target)-1
            self._cache[index][target-BOUNDARIES[index]] = byte

    def _mapped(self, address, count, *, cleanup=False):
        result = bytearray()
        for offset in range(count):
            if not cleanup:
                self._checkpoint()
            result.extend(self._session.mapped_bytes(address+offset, 1))
            if (offset+1) % 16 == 0:
                self._step(f'Exact RAM readback: {offset+1}/{count} bytes at 0x{address:06X}.')
        return bytes(result)

    def register_table(self, definition, include_axes=True):
        def action(report):
            report['spans'] = self._register(definition, include_axes)
            self._step('Complete table/axis blocks are registered within the verified RAM budget.')
        return self._run('register_table', action)

    def _apply(self, definition, edited, include_axes, report):
        if not isinstance(edited, bytes) or len(edited) != len(self.original):
            raise ValueError('Capture the complete current BIN before uploading this table.')
        spans = self._register(definition, include_axes)
        report['spans'] = spans
        self._select('working')
        session = self._session
        session.live_write_spans = [(s['logical_address'], s['logical_address']+s['bytes']) for s in spans]
        changes = []
        for span in spans:
            start, count, logical = span['file_offset'], span['bytes'], span['logical_address']
            desired = edited[start:start+count]
            cached = self._cached(logical, count)
            offset = 0
            while offset < count:
                if desired[offset] == cached[offset]:
                    offset += 1
                    continue
                begin = offset
                boundary = BOUNDARIES[bisect_right(BOUNDARIES, logical+offset)]
                while (offset < count and desired[offset] != cached[offset]
                       and offset-begin < 250 and logical+offset < boundary):
                    offset += 1
                changes.append((logical+begin, cached[begin:offset], desired[begin:offset]))
        report['details'].update(changed_bytes=sum(len(new) for _, _, new in changes),
                                 source_sha256=hashlib.sha256(edited).hexdigest(),
                                 multi_cell_update_atomic=False)
        attempted = []
        try:
            for address, expected_old, new in changes:
                self._checkpoint()
                self._state()
                actual_old = self._mapped(address, len(new))
                if actual_old != expected_old:
                    raise SerialCommunicationError('RAM differs from this connection’s verified edit history; upload stopped.')
                attempted.append((address, actual_old))
                report['details']['rollback_bytes'] = [dict(address=a, hex=b.hex().upper()) for a, b in attempted]
                report['restore']['write'] = 'required'
                session.write_managed(address, new)
                if self._mapped(address, len(new)) != new:
                    raise SerialCommunicationError('Changed RAM bytes did not read back exactly.')
                self._cache_put(address, new)
                self._step(f'Uploaded and exactly verified {len(new)} bytes at 0x{address:06X}.')
            # Include cancellation and state validation in the rollback scope,
            # including a cancel requested by the final progress callback.
            self._checkpoint()
            self._state()
            report['restore']['write'] = 'retained_verified' if changes else 'not_needed'
            report['details']['working_changes_retained'] = bool(changes)
        except Exception:
            restored = not attempted
            if attempted and not session.faulted:
                try:
                    for address, original in reversed(attempted):
                        self._state()
                        session.write_managed(address, original)
                        if self._mapped(address, len(original), cleanup=True) != original:
                            raise SerialCommunicationError('Original RAM bytes failed exact rollback readback.')
                        self._state()
                        self._cache_put(address, original)
                    restored = True
                except Exception as error:
                    report['details']['rollback_error'] = str(error)
            report['restore']['write'] = 'verified' if restored else 'unknown'
            if not restored:
                report['restore'].update(restoration_required=True, power_cycle_required=True)
            raise
        finally:
            session.live_write_payload = None
            session.live_write_spans = []

    def apply(self, definition, edited, include_axes=True):
        definition = deepcopy(definition)
        return self._run('apply', lambda report: self._apply(definition, edited, include_axes, report))

    def read(self, definition, include_axes=True):
        def action(report):
            spans = self._register(definition, include_axes)
            self._select('working')
            updates = []
            for span in spans:
                raw = self._mapped(span['logical_address'], span['bytes'])
                self._cache_put(span['logical_address'], raw)
                updates.append(dict(file_offset=span['file_offset'], hex=raw.hex().upper()))
            self._checkpoint()
            self._state()
            report.update(updates=updates, spans=spans)
            self._step('Exact working-page table and axis bytes read; the GUI can apply these as one undoable edit.')
        return self._run('read', action)

    def restore(self, definition, include_axes=True):
        return self._run('restore', lambda report: self._apply(deepcopy(definition), self.original, include_axes, report))

    def select_page(self, page):
        return self._run('select_page', lambda report: (self._select(page),
            self._step(f'Application calibration page verified: {page}.')))

    def disconnect(self):
        report = self._new_report('disconnect')
        report['status'] = 'passed'
        self._shutdown(report)
        if report['restore']['reference_page'] == 'unknown':
            report.update(status='failed', error=report['error'] or 'Reference-page restoration is unconfirmed.')
        return self._details(report)
