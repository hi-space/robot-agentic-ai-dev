import os, time, cv2
import argparse
import asyncio
import boto3
import json
import platform
import websockets
import threading
import queue
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRelay
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame
from base64 import b64decode, b64encode
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
from fractions import Fraction
import sys
import logging

# ---------------- Logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), stream=sys.stdout)
logger = logging.getLogger("kvswebrtc_autorun")

def _must_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env: {name}")
    return v

# ---------------- Environment ----------------
AWS_DEFAULT_REGION = _must_env("AWS_DEFAULT_REGION")
CHANNEL_ARN = _must_env("CHANNEL_ARN")
VIDEO_DEVICE_DEFAULT = os.getenv("VIDEO_DEVICE", "/dev/video4")
FILE_PATH_DEFAULT = os.getenv("FILE_PATH", "")

FRAME_RATE = os.getenv('FRAME_RATE', '18')
VIDEO_SIZE = os.getenv('VIDEO_SIZE', '640x480')  # WebRTC 전송용 해상도
# VIDEO_SIZE = os.getenv('VIDEO_SIZE', '1280x720')
CAPTURE_VIDEO_SIZE = os.getenv('CAPTURE_VIDEO_SIZE', '1280x720')  # 캡처/저장용 해상도
SAVE_INTERVAL = float(os.getenv('SAVE_INTERVAL', '1.0'))
SAVE_QUEUE_SIZE = int(os.getenv('SAVE_QUEUE_SIZE', '30'))

# 새로 추가: GStreamer 사용 플래그 / 사용자 파이프라인
USE_GSTREAMER = os.getenv('USE_GSTREAMER', '0').lower() in ('1', 'true', 'yes')
GST_PIPELINE = os.getenv('GST_PIPELINE', '')  # 있으면 우선 사용

# 해상도 파싱 함수
def parse_video_size(size_str):
    try:
        w, h = map(int, size_str.split('x'))
        return w, h
    except Exception:
        logger.warning(f"Invalid video size '{size_str}', falling back to defaults")
        return 640, 480

STREAM_WIDTH, STREAM_HEIGHT = parse_video_size(VIDEO_SIZE)

# ---------------- Video Streaming Track (WebRTC 전송용 다운스케일링) ----------------
class StreamingVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, source_track, target_size=(640, 480)):
        super().__init__()
        self.source = source_track
        self.target_width, self.target_height = target_size
        logger.info(f"[StreamingVideoTrack] target_size={target_size}")

    async def recv(self):
        frame: VideoFrame = await self.source.recv()
        
        # 원본 프레임을 타겟 해상도로 다운스케일링
        if frame.width != self.target_width or frame.height != self.target_height:
            img = frame.to_ndarray(format="bgr24")
            resized_img = cv2.resize(img, (self.target_width, self.target_height))
            new_frame = VideoFrame.from_ndarray(resized_img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        
        return frame

    def stop(self):
        try:
            if self.source:
                sstop = getattr(self.source, "stop", None)
                if callable(sstop):
                    r = sstop()
                    if asyncio.iscoroutine(r):
                        asyncio.create_task(r)
        except Exception as e:
            logger.warning(f"[StreamingVideoTrack] ERROR during stop: {e}")
        finally:
            try:
                super().stop()
            except Exception as e:
                logger.warning(f"[StreamingVideoTrack] super().stop() error: {e}")

# ---------------- Video Saving Track ----------------
class SavingVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, source_track, interval=1.0, save_dir="/home/unitree/captured_frames",
                 filename_prefix="frame_", ext=".jpg", queue_size=30):
        super().__init__()
        self.source = source_track
        self.interval = float(interval)
        self.save_dir = os.path.abspath(save_dir)
        self.prefix = filename_prefix
        self.ext = ext if ext.startswith(".") else "." + ext
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir, exist_ok=True)

        self._last = 0.0
        self._seq = 0

        self._save_queue = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._save_worker, daemon=True)
        self._worker.start()

        self._prev_ts = None
        self._deltas = []
        self._drops = 0
        self._processed = 0
        self._warned_queue_full = 0

        logger.info(f"[SavingVideoTrack] save_dir={self.save_dir}, interval={self.interval}s, q={queue_size}")

    def _save_worker(self):
        while not self._stop_event.is_set():
            try:
                img, fname = self._save_queue.get(timeout=0.5)
                cv2.imwrite(fname, img)
                logger.debug(f"[SavingVideoTrack] saved {fname}")
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"[SavingVideoTrack] ERROR saving frame: {e}")

    async def recv(self):
        frame: VideoFrame = await self.source.recv()
        now = time.monotonic()

        if self._prev_ts is not None:
            delta = now - self._prev_ts
            self._deltas.append(delta)
            if len(self._deltas) >= 60:
                avg = sum(self._deltas) / len(self._deltas)
                logger.info(f"[SavingVideoTrack][stats] frames={self._processed} drops={self._drops} "
                            f"queue_full_warn={self._warned_queue_full} "
                            f"delta_avg={avg:.4f}s min={min(self._deltas):.4f}s max={max(self._deltas):.4f}s "
                            f"expected~{1/float(FRAME_RATE):.4f}s")
                self._deltas.clear()
        self._prev_ts = now

        if (now - self._last) >= self.interval:
            self._last = now
            self._seq += 1
            self._processed += 1
            try:
                img = frame.to_ndarray(format="bgr24")
                fname = os.path.join(self.save_dir, f"{self.prefix}{self._seq:05d}{self.ext}")
                if not self._save_queue.full():
                    self._save_queue.put_nowait((img, fname))
                else:
                    self._drops += 1
                    self._warned_queue_full += 1
                    if self._warned_queue_full <= 10 or self._warned_queue_full % 50 == 0:
                        logger.warning(f"[SavingVideoTrack] queue full (size={self._save_queue.maxsize}) "
                                       f"drops={self._drops} (suppressing logs)")
            except Exception as e:
                logger.error(f"[SavingVideoTrack] ERROR queueing frame: {e}")
        return frame

    def stop(self):
        self._stop_event.set()
        try:
            self._worker.join(timeout=2)
        except Exception as e:
            logger.warning(f"[SavingVideoTrack] worker join error: {e}")
        try:
            if self.source:
                sstop = getattr(self.source, "stop", None)
                if callable(sstop):
                    r = sstop()
                    if asyncio.iscoroutine(r):
                        asyncio.create_task(r)
        except Exception as e:
            logger.warning(f"[SavingVideoTrack] ERROR during stop: {e}")
        finally:
            try:
                super().stop()
            except Exception as e:
                logger.warning(f"[SavingVideoTrack] super().stop() error: {e}")

# ---------------- Media Tracks ----------------
class MediaTrackManager:
    def __init__(self, file_path=None, video_device=None):
        self.file_path = file_path
        self.video_device = video_device

    def create_media_track(self):
        relay = MediaRelay()
        options = {'framerate': FRAME_RATE, 'video_size': CAPTURE_VIDEO_SIZE}
        system = platform.system()

        if self.file_path and not os.path.exists(self.file_path):
            raise FileNotFoundError(f"The file {self.file_path} does not exist.")

        if system == 'Darwin':
            if not self.file_path:
                device = self.video_device if self.video_device else 'default:default'
                media = MediaPlayer(device, format='avfoundation', options=options)
            else:
                media = MediaPlayer(self.file_path)
        elif system == 'Windows':
            if not self.file_path:
                device = self.video_device if self.video_device else 'video=Integrated Camera'
                media = MediaPlayer(device, format='dshow', options=options)
            else:
                media = MediaPlayer(self.file_path)
        elif system == 'Linux':
            # 변경: GStreamer 사용 옵션 추가 (환경변수 USE_GSTREAMER=1 또는 GST_PIPELINE 제공)
            w, h = (1280, 720)
            try:
                if 'x' in CAPTURE_VIDEO_SIZE:
                    w, h = map(int, CAPTURE_VIDEO_SIZE.split('x', 1))
            except Exception:
                logger.warning(f"Invalid CAPTURE_VIDEO_SIZE '{CAPTURE_VIDEO_SIZE}', falling back to 1280x720")

            if GST_PIPELINE:
                gst_pipeline = GST_PIPELINE
                logger.info(f"Using GST_PIPELINE from env: {gst_pipeline}")
            elif USE_GSTREAMER:
                device = self.video_device if self.video_device else VIDEO_DEVICE_DEFAULT
                if not self.file_path:
                    # 카메라 입력용 기본 파이프라인 (필요시 하드웨어 플러그인으로 조정)
                    gst_pipeline = (
                        f"v4l2src device={device} ! "
                        f"video/x-raw, width={w}, height={h}, framerate={FRAME_RATE}/1 ! "
                        f"videoconvert ! videoscale ! "
                        f"video/x-raw, format=I420 ! appsink"
                    )
                else:
                    # 파일 재생용 기본 파이프라인 (decodebin이 하드웨어 가속 플러그인을 사용하도록 환경을 맞춰야 함)
                    gst_pipeline = (
                        f"filesrc location={self.file_path} ! decodebin ! "
                        f"videoconvert ! videoscale ! "
                        f"video/x-raw, format=I420, width={w}, height={h}, framerate={FRAME_RATE}/1 ! appsink"
                    )
                logger.info(f"Constructed GStreamer pipeline: {gst_pipeline}")
            else:
                gst_pipeline = ''

            if gst_pipeline:
                try:
                    # format='gst'으로 GStreamer 파이프라인을 열기 시도
                    media = MediaPlayer(gst_pipeline, format='gst')
                except Exception as e:
                    logger.warning(f"GStreamer MediaPlayer failed ({e}), falling back to default MediaPlayer")
                    if not self.file_path:
                        device = self.video_device if self.video_device else VIDEO_DEVICE_DEFAULT
                        media = MediaPlayer(device, format='v4l2', options=options)
                    else:
                        media = MediaPlayer(self.file_path)
            else:
                # 기존 동작: v4l2 또는 파일
                if not self.file_path:
                    device = self.video_device if self.video_device else VIDEO_DEVICE_DEFAULT
                    media = MediaPlayer(device, format='v4l2', options=options)
                else:
                    media = MediaPlayer(self.file_path)
        else:
            raise NotImplementedError(f"Unsupported platform: {system}")

        audio_track = relay.subscribe(media.audio) if media.audio else None
        video_track = relay.subscribe(media.video) if media.video else None

        if audio_track is None and video_track is None:
            raise ValueError("Neither audio nor video track could be created from the source.")

        return audio_track, video_track

# ---------------- KVS WebRTC Client ----------------
class KinesisVideoClient:
    def __init__(self, client_id, region, channel_arn, credentials, file_path=None):
        self.client_id = client_id
        self.region = region
        self.channel_arn = channel_arn
        self.credentials = credentials  # always None (use boto3 default chain)
        self.media_manager = MediaTrackManager(file_path, video_device=VIDEO_DEVICE_DEFAULT)
        if self.credentials:
            self.kinesisvideo = boto3.client('kinesisvideo',
                                             region_name=self.region,
                                             aws_access_key_id=self.credentials['accessKeyId'],
                                             aws_secret_access_key=self.credentials['secretAccessKey'],
                                             aws_session_token=self.credentials['sessionToken'])
        else:
            self.kinesisvideo = boto3.client('kinesisvideo', region_name=self.region)
        self.endpoints = None
        self.endpoint_https = None
        self.endpoint_wss = None
        self.ice_servers = None
        self.PCMap = {}
        self.DCMap = {}
        self.video_relay = MediaRelay()
        self.original_video_track = None
        self.viewer_tracks = {}
        self.saving_track = None
        self.saving_task = None

    def get_signaling_channel_endpoint(self):
        if self.endpoints is None:
            endpoints = self.kinesisvideo.get_signaling_channel_endpoint(
                ChannelARN=self.channel_arn,
                SingleMasterChannelEndpointConfiguration={'Protocols': ['HTTPS', 'WSS'], 'Role': 'MASTER'}
            )
            self.endpoints = {
                'HTTPS': next(o['ResourceEndpoint'] for o in endpoints['ResourceEndpointList'] if o['Protocol'] == 'HTTPS'),
                'WSS': next(o['ResourceEndpoint'] for o in endpoints['ResourceEndpointList'] if o['Protocol'] == 'WSS')
            }
            self.endpoint_https = self.endpoints['HTTPS']
            self.endpoint_wss = self.endpoints['WSS']
        return self.endpoints

    def prepare_ice_servers(self):
        if self.credentials:
            kinesis_video_signaling = boto3.client('kinesis-video-signaling',
                                                   endpoint_url=self.endpoint_https,
                                                   region_name=self.region,
                                                   aws_access_key_id=self.credentials['accessKeyId'],
                                                   aws_secret_access_key=self.credentials['secretAccessKey'],
                                                   aws_session_token=self.credentials['sessionToken'])
        else:
            kinesis_video_signaling = boto3.client('kinesis-video-signaling',
                                                   endpoint_url=self.endpoint_https,
                                                   region_name=self.region)
        ice_server_config = kinesis_video_signaling.get_ice_server_config(
            ChannelARN=self.channel_arn,
            ClientId='MASTER'
        )

        iceServers = [RTCIceServer(urls=f'stun:stun.kinesisvideo.{self.region}.amazonaws.com:443')]
        for iceServer in ice_server_config.get('IceServerList', []):
            iceServers.append(RTCIceServer(
                urls=iceServer['Uris'],
                username=iceServer.get('Username'),
                credential=iceServer.get('Password')
            ))
        self.ice_servers = iceServers
        return self.ice_servers

    def create_wss_url(self):
        # Use boto3/botocore default credential chain (no IoT provider, no IPC)
        session = Session()
        auth_credentials = session.get_credentials()
        SigV4 = SigV4QueryAuth(auth_credentials, 'kinesisvideo', self.region, 299)
        aws_request = AWSRequest(
            method='GET',
            url=self.endpoint_wss,
            params={'X-Amz-ChannelARN': self.channel_arn, 'X-Amz-ClientId': self.client_id}
        )
        SigV4.add_auth(aws_request)
        PreparedRequest = aws_request.prepare()
        return PreparedRequest.url

    def decode_msg(self, msg):
        try:
            data = json.loads(msg)
            payload = json.loads(b64decode(data['messagePayload'].encode('ascii')).decode('ascii'))
            return data['messageType'], payload, data.get('senderClientId')
        except json.decoder.JSONDecodeError:
            return '', {}, ''

    def encode_msg(self, action, payload, client_id):
        return json.dumps({
            'action': action,
            'messagePayload': b64encode(json.dumps(payload.__dict__).encode('ascii')).decode('ascii'),
            'recipientClientId': client_id,
        })

    async def _saving_loop(self):
        logger.info("[MASTER] saving loop started")
        try:
            while self.saving_track:
                try:
                    await self.saving_track.recv()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[MASTER] saving loop error: {e}")
                    await asyncio.sleep(0.2)
        finally:
            logger.info("[MASTER] saving loop stopped")

    async def _cleanup_peer(self, client_id):
        pc = self.PCMap.get(client_id)
        if pc:
            try:
                await pc.close()
            except Exception as e:
                logger.warning(f"[{client_id}] PC close error: {e}")
            self.PCMap.pop(client_id, None)
        self.DCMap.pop(client_id, None)

        vtrack = self.viewer_tracks.pop(client_id, None)
        if vtrack:
            try:
                stop_fn = getattr(vtrack, "stop", None)
                if callable(stop_fn):
                    stop_fn()
            except Exception as e:
                logger.warning(f"[{client_id}] viewer track stop error: {e}")

        if not self.PCMap:
            logger.info("[MASTER] all viewers disconnected (saving continues)")

    async def handle_sdp_offer(self, payload, client_id, audio_track, video_track, websocket):
        iceServers = self.prepare_ice_servers()
        configuration = RTCConfiguration(iceServers=iceServers)
        pc = RTCPeerConnection(configuration=configuration)
        self.DCMap[client_id] = pc.createDataChannel('kvsDataChannel')
        self.PCMap[client_id] = pc

        @pc.on('connectionstatechange')
        async def on_connectionstatechange():
            state = self.PCMap.get(client_id).connectionState if client_id in self.PCMap else "closed"
            logger.info(f'[{client_id}] connectionState: {state}')
            if state in ("failed", "closed", "disconnected"):
                await self._cleanup_peer(client_id)

        @pc.on('iceconnectionstatechange')
        async def on_iceconnectionstatechange():
            if client_id in self.PCMap:
                logger.info(f'[{client_id}] iceConnectionState: {self.PCMap[client_id].iceConnectionState}')

        @pc.on('icegatheringstatechange')
        async def on_icegatheringstatechange():
            if client_id in self.PCMap:
                logger.info(f'[{client_id}] iceGatheringState: {self.PCMap[client_id].iceGatheringState}')

        @pc.on('signalingstatechange')
        async def on_signalingstatechange():
            if client_id in self.PCMap:
                logger.info(f'[{client_id}] signalingState: {self.PCMap[client_id].signalingState}')

        @pc.on('track')
        def on_track(track):
            MediaBlackhole().addTrack(track)

        if audio_track:
            pc.addTrack(audio_track)

        if video_track:
            if self.original_video_track is None:
                self.original_video_track = video_track
            
            # WebRTC 전송용: 다운스케일링된 트랙 생성
            base_track = self.video_relay.subscribe(self.original_video_track)
            streaming_track = StreamingVideoTrack(base_track, target_size=(STREAM_WIDTH, STREAM_HEIGHT))
            self.viewer_tracks[client_id] = streaming_track
            pc.addTrack(streaming_track)
            logger.info(f"[{client_id}] viewer video track added (streaming: {STREAM_WIDTH}x{STREAM_HEIGHT})")

            if self.saving_track is None:
                base_for_saving = self.video_relay.subscribe(self.original_video_track)
                self.saving_track = SavingVideoTrack(
                    base_for_saving,
                    interval=SAVE_INTERVAL,
                    save_dir="/home/unitree/captured_frames",
                    queue_size=SAVE_QUEUE_SIZE
                )
                self.saving_task = asyncio.create_task(self._saving_loop())
                logger.info("[MASTER] frame saving started")

        await pc.setRemoteDescription(RTCSessionDescription(
            sdp=payload['sdp'],
            type=payload['type']
        ))
        await pc.setLocalDescription(await pc.createAnswer())
        await websocket.send(self.encode_msg('SDP_ANSWER', pc.localDescription, client_id))

    async def handle_ice_candidate(self, payload, client_id):
        pc = self.PCMap.get(client_id)
        if not pc:
            logger.info(f"[{client_id}] ICE candidate ignored (no PC).")
            return
        try:
            candidate = candidate_from_sdp(payload['candidate'])
            candidate.sdpMid = payload.get('sdpMid')
            candidate.sdpMLineIndex = payload.get('sdpMLineIndex')
            await pc.addIceCandidate(candidate)
            logger.info(f"[{client_id}] ICE candidate added.")
        except Exception as e:
            logger.warning(f"[{client_id}] addIceCandidate error: {e}")

    async def signaling_client(self):
        audio_track, video_track = self.media_manager.create_media_track()

        # Optional: start saving even before any viewer
        if video_track and self.saving_track is None:
            if self.original_video_track is None:
                self.original_video_track = video_track
            base_for_saving = self.video_relay.subscribe(self.original_video_track)
            self.saving_track = SavingVideoTrack(
                base_for_saving,
                interval=SAVE_INTERVAL,
                save_dir="/home/unitree/captured_frames",
                queue_size=SAVE_QUEUE_SIZE
            )
            self.saving_task = asyncio.create_task(self._saving_loop())
            logger.info("[MASTER] frame saving started (before any viewer)")

        self.get_signaling_channel_endpoint()
        wss_url = self.create_wss_url()

        while True:
            try:
                async with websockets.connect(wss_url) as websocket:
                    logger.info('Signaling Server Connected!')
                    async for message in websocket:
                        msg_type, payload, client_id = self.decode_msg(message)
                        if msg_type == 'SDP_OFFER':
                            await self.handle_sdp_offer(payload, client_id, audio_track, video_track, websocket)
                        elif msg_type == 'ICE_CANDIDATE':
                            await self.handle_ice_candidate(payload, client_id)
            except websockets.ConnectionClosed:
                logger.info('Connection closed, reconnecting...')
                wss_url = self.create_wss_url()
                continue
            except Exception as e:
                logger.warning(f"Signaling loop error: {e}")
                await asyncio.sleep(1.0)

# ---------------- Entrypoint ----------------
async def run_client(client):
    await client.signaling_client()

async def main():
    # argparse는 유지하되 env 우선
    parser = argparse.ArgumentParser(description='Kinesis Video Streams WebRTC Client (env-driven, no IPC/IoT provider)')
    parser.add_argument('--channel-arn', type=str, help='Signaling channel ARN (optional; env CHANNEL_ARN preferred)')
    parser.add_argument('--file-path', type=str, help='Video file path (optional; env FILE_PATH)')
    parser.add_argument('--video-device', type=str, help='Video device (optional; env VIDEO_DEVICE)')
    args = parser.parse_args()

    region = AWS_DEFAULT_REGION
    channel_arn = args.channel_arn or CHANNEL_ARN
    if not channel_arn:
        channel_arn = _must_env("CHANNEL_ARN")

    video_device = args.video_device or VIDEO_DEVICE_DEFAULT
    file_path = args.file_path or (FILE_PATH_DEFAULT if FILE_PATH_DEFAULT else None)

    # boto3 기본 자격증명 체인 사용 → credentials=None
    client = KinesisVideoClient(
        client_id="MASTER",
        region=region,
        channel_arn=channel_arn,
        credentials=None,
        file_path=file_path
    )

    await run_client(client)

if __name__ == '__main__':
    asyncio.run(main())