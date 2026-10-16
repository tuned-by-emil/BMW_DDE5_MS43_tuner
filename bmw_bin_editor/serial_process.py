"""Keep UART deadlines independent of the GUI process's Python interpreter."""
from __future__ import annotations

import multiprocessing
import time
from .serial_transport import KlineSession, SerialCancelled, SerialCommunicationError, validate_port


def _serve(connection, cancelled, port, echo, session_factory):
    try:
        if cancelled.is_set():
            return
        with session_factory(port, echo=echo, cancelled=cancelled.is_set) as session:
            connection.send(('okay', None))
            while not cancelled.is_set():
                if not connection.poll(.05):
                    continue
                command, argument = connection.recv()
                if command == 'close':
                    break
                if command == 'identify':
                    result = session.identify()
                elif command == 'measurements':
                    result = session.read_measurements(argument)
                else:
                    raise SerialCommunicationError('Unsupported serial process operation.')
                connection.send(('okay', result))
    except SerialCancelled as error:
        try:
            connection.send(('cancelled', str(error)))
        except (OSError, EOFError):
            pass
    except Exception as error:
        try:
            connection.send(('error', str(error)))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


class ProcessKlineSession:
    """One child owns COM; the Qt worker owns cancellation and final cleanup."""

    # Covers startup and the transport's two bounded 5.02-second pending waits.
    PROCESS_WAIT_SECONDS = 12.0

    def __init__(self, port, *, echo=True, cancelled=None, session_factory=KlineSession):
        self.port_name = validate_port(port)
        if type(echo) is not bool:
            raise ValueError('Cable echo must be enabled or disabled.')
        self.echo = echo
        self.cancelled = cancelled or (lambda: False)
        self.session_factory = session_factory
        self.connection = self.process = self.cancel_event = None
        self.faulted = False

    def _check_cancelled(self):
        if self.cancelled():
            if self.cancel_event is not None:
                self.cancel_event.set()
            raise SerialCancelled('Serial read cancelled.')

    def __enter__(self):
        if self.process is not None:
            raise SerialCommunicationError('Serial session is already open.')
        self._check_cancelled()
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        self.connection = parent
        self.cancel_event = context.Event()
        self.process = context.Process(target=_serve,
            args=(child, self.cancel_event, self.port_name, self.echo, self.session_factory),
            name='BMW serial reader', daemon=True)
        try:
            self.process.start()
            child.close()
            self._receive()
            return self
        except Exception:
            child.close()
            self.close()
            raise

    def _receive(self):
        deadline = time.monotonic() + self.PROCESS_WAIT_SECONDS
        while True:
            self._check_cancelled()
            # A queued result retains the UART checks already performed in the
            # child, even if a GUI redraw delayed this process's scheduling.
            if self.connection.poll(.010):
                try:
                    status, result = self.connection.recv()
                except (EOFError, OSError) as error:
                    raise SerialCommunicationError('Serial reader process closed unexpectedly.') from error
                if status == 'okay':
                    return result
                self.faulted = True
                if status == 'cancelled':
                    raise SerialCancelled(result)
                raise SerialCommunicationError(result)
            if not self.process.is_alive():
                raise SerialCommunicationError('Serial reader process exited unexpectedly.')
            if time.monotonic() >= deadline:
                self.faulted = True
                raise SerialCommunicationError('Serial reader process did not respond within 12 seconds.')

    def _request(self, command, argument=None):
        self._check_cancelled()
        if self.connection is None or self.faulted:
            raise SerialCommunicationError('Serial session is closed or failed; reconnect before further requests.')
        try:
            self.connection.send((command, argument))
            return self._receive()
        except (OSError, EOFError) as error:
            self.faulted = True
            raise SerialCommunicationError('Serial reader process communication failed.') from error
        except Exception:
            self.faulted = True
            raise

    def identify(self):
        return self._request('identify')

    def read_measurements(self, ids):
        return self._request('measurements', tuple(ids))

    def __exit__(self, *_):
        self.close()

    def close(self):
        process, connection = self.process, self.connection
        self.process = self.connection = None
        if self.cancel_event is not None:
            self.cancel_event.set()
        if connection is not None:
            try:
                connection.send(('close', None))
            except (OSError, EOFError):
                pass
        if process is not None and process.pid is not None:
            process.join(1.5)
            if process.is_alive():
                process.terminate()
                process.join(1.5)
            if process.is_alive():
                raise SerialCommunicationError('Could not stop the serial reader process.')
            process.close()
        if connection is not None:
            connection.close()
