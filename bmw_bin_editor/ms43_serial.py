"""MS43X001 DS2 over a transparent K-line cable, bounded and cancellable.

Only the ADX's baud-change and extended measurement commands are sent.
No initialization pulse, programming session, ECU reset or calibration write.
"""
import time
from .serial_transport import SerialCommunicationError, SerialCancelled, SerialReply, validate_port, xor_checksum
from .ms43_logging_definition import DATA_REQUEST, FAST_REQUEST, SLOW_REQUEST, ACK, DATA_HEADER, FRAME_SIZE


class MS43Session:
    def __init__(self,port,*,echo=True,initial_baud=9600,cancelled=None,serial_factory=None,
                 trace=None,clock=time.perf_counter,sleep=time.sleep):
        self.port=validate_port(port)
        if type(echo) is not bool:raise ValueError('Cable echo must be enabled or disabled.')
        if initial_baud not in (9600,125000):raise ValueError('MS43X initial baud must be 9600 or 125000.')
        self.echo=echo; self.initial_baud=initial_baud
        self.cancelled=cancelled or (lambda:False)
        self.serial_factory=serial_factory;self.trace=trace or (lambda **kw:None)
        self.clock=clock;self.sleep=sleep;self.connection=None
        self.fast=False;self.cleanup_error=None;self.cleaning=False;self.faulted=False
        self.last_reply=None

    def _check(self):
        if not self.cleaning and self.cancelled():raise SerialCancelled('MS43X logging cancelled.')

    def __enter__(self):
        self._check()
        factory=self.serial_factory
        if factory is None:
            import serial
            factory=serial.Serial
        try:
            self.connection=factory(port=None,baudrate=self.initial_baud,bytesize=8,parity='E',stopbits=1,
                                    timeout=0.010,write_timeout=0.500,xonxoff=False,rtscts=False,dsrdtr=False)
            self.connection.port=self.port
            self.connection.dtr=False;self.connection.rts=False
            self._check();self.connection.open();self.connection.reset_input_buffer()
            self.trace(event='open',port=self.port,baud=self.initial_baud,format='8E1',echo=self.echo)
            if self.initial_baud==9600:
                self._exchange(FAST_REQUEST,ACK)
                self.fast=True  # ECU acknowledged; cleanup must try the new baud even if local switching fails.
                self.connection.baudrate=125000
                self.trace(event='baud',baud=125000)
            else:
                self.fast=True
            return self
        except Exception:
            self.close()
            raise

    def _read_exact(self,count,first_timeout):
        data=bytearray();deadline=self.clock()+first_timeout
        while len(data)<count:
            self._check()
            if self.clock()>=deadline:raise SerialCommunicationError(f'MS43X serial timeout ({len(data)}/{count} bytes).')
            part=self.connection.read(count-len(data))
            if part:
                if len(part)>count-len(data):raise SerialCommunicationError('MS43X serial reader returned too many bytes.')
                data.extend(part);deadline=self.clock()+0.040
                self.trace(event='rx_chunk',baud=self.connection.baudrate,hex=part.hex().upper())
            else:self.sleep(0.001)
        return bytes(data)

    def _exchange(self,request,expected_ack=None):
        self._check()
        if self.connection is None or not self.connection.is_open:raise SerialCommunicationError('MS43X session is closed.')
        if request not in (DATA_REQUEST,FAST_REQUEST,SLOW_REQUEST):raise ValueError('Unsupported MS43X request.')
        if self.faulted and not self.cleaning:raise SerialCommunicationError('MS43X session failed; reconnect before retrying.')
        # Separate requests by at least 10 ms; no queued/overlapping transactions.
        if self.last_reply is not None:
            due=self.last_reply+0.010
            while self.clock()<due:
                self._check();self.sleep(min(0.005,due-self.clock()))
        self.connection.reset_input_buffer()
        self.trace(event='tx',baud=self.connection.baudrate,hex=request.hex().upper())
        try:
            if self.connection.write(request)!=len(request):raise SerialCommunicationError('Incomplete MS43X serial write.')
            if self.echo:
                echoed=self._read_exact(len(request),0.500)
                if echoed!=request:raise SerialCommunicationError('Cable echo differs from the request; check the cable echo option.')
            header=self._read_exact(2,0.500 if expected_ack else 0.400)
            if header[0]!=0x12 or header[1] not in (4,FRAME_SIZE):
                raise SerialCommunicationError('Unexpected MS43X DS2 address/length: '+header.hex().upper())
            frame=header+self._read_exact(header[1]-2,0.040)
            self.trace(event='rx_frame',baud=self.connection.baudrate,hex=frame.hex().upper())
            if xor_checksum(frame):raise SerialCommunicationError('MS43X response XOR checksum mismatch.')
            if expected_ack is not None:
                if frame!=expected_ack:raise SerialCommunicationError('MS43X ECU did not acknowledge baud-rate change.')
            elif len(frame)!=FRAME_SIZE or frame[:3]!=DATA_HEADER:
                raise SerialCommunicationError('Unexpected MS43X extended logging response.')
            self.last_reply=self.clock()
            return SerialReply(frame[3:-1],request,frame)
        except Exception as exc:
            self.faulted=True
            self.trace(event='error',message=str(exc))
            raise

    def read(self):
        return self._exchange(DATA_REQUEST)

    def close(self):
        connection=self.connection
        if connection is None:return
        self.cleaning=True
        try:
            if connection.is_open and self.fast:
                connection.baudrate=125000
                self._exchange(SLOW_REQUEST,ACK)
                connection.baudrate=9600
                self.trace(event='baud',baud=9600)
                self.fast=False
        except Exception as exc:
            self.cleanup_error=str(exc)
            try:self.trace(event='cleanup_error',message=str(exc))
            except Exception:pass
        finally:
            try:connection.close()
            finally:
                self.connection=None;self.cleaning=False

    def __exit__(self,*args):
        self.close()
