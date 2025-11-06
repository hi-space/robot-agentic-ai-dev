#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import time
import sys
import threading
import signal
from collections import deque

from loguru import logger
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.sport.sport_client import SportClient

# ===== Greengrass v2 IPC (IoT Core 브리지) =====
from awsiot.greengrasscoreipc.clientv2 import GreengrassCoreIPCClientV2
from awsiot.greengrasscoreipc.model import QOS

# ===== (선택) Polly TTS =====
import math
import boto3
import base64
import subprocess

MQTT_GESTURE_TOPIC = 'data/robot/gesture'
# MQTT 토픽
MQTT_CMD_TOPIC      = "robot/control"
MQTT_CMD_GESTURE    = "data/edge/gesture"
MQTT_RESULT_TOPIC   = "robot/result"

# 기본 파라미터
LIN_SPEED = 0.42
YAW_SPEED = 0.52
MOVE_DURATION = 5.0 # 이동 명령 유지 시간
TURN_DURATION = 2.0
TOGGLE_ACTION_DURATION = 2.0

POST_STAND_WAKE_UP_DURATION = 0.5

EMERGENCY_BRAKE_PULSES   = 3
EMERGENCY_BRAKE_INTERVAL = 0.05
FORWARD_L = 1.0                  # 배율 1 (기준)
FORWARD_S = 1.0 / (2*math.cos(math.radians(15)))  # 배율 0.54 (약 1/2)


class RobotController:
    """
    - 이동 시간(MOVE_DURATION) 분리
    - 앉은 상태 자동 기립
    - Greengrass IPC 로 IoT Core 구독/발행
    - 제스처 토픽은 on/off 시 IPC 구독을 열고/닫음 (paho 제거)
    """
    def __init__(self):
        # ===== Unitree client =====
        self.bot = SportClient()
        self.bot.SetTimeout(20.0)
        self.bot.Init()

        # ===== 상태 =====
        self.lock = threading.Lock()
        self.is_sitting = False
        self.seq_id = 0
        self.seq_running = False
        self.interrupt = threading.Event()
        self.interrupt_reason = None

        self.current_sequence = []
        self.conducted = []
        self.error_offset = []
        self.custom_move_duration = MOVE_DURATION
        self.custom_move_speed = LIN_SPEED
        self.safe_mode = False
        
        self.say_ = None
        self.saying_switch = False  # 말하기 스위치
        self.pending = deque()

        self.gesture_on_area_move_command = "from1to2"
        self.gesture_on_area_test = "test_gesture"

        self.CUSTOM_OPERATION_LIST = {
            'detected': ['hello'],
            'from0to1': ['turn_right','turn_right', 'forward_L'],
            'from1to2': ['turn_left','turn_left','turn_left_S','turn_left_S', 'forward_S', 'turn_right_S', 'turn_right_S'],
            'from2to0': ['turn_left_S','turn_left','turn_left', 'forward_S', 'turn_right_S','turn_right','turn_right'],
            'hi' : ['hello'],
            'normal': ['stretch'],
            'heart': ['heart_'],
            'test_gesture': ['sit','stand']
        }
        self.othermove_gesture = [ 'from2to0', 'from0to1']
        self.gesture_switch = False
        # 제스처 매핑
        self.GESTURE_ACTION_MAP = {
            "heart":        {"move": ["heart_"], "say": "저도 사랑해요!! 좋은 하루 되세요!"},
            "X":            {"move": ["sit"],    "say": "네, 알겠습니다. 문제가 생겼군요!"},
            "O":            {"move": ["hello"],  "say": "네, 아무 문제 없군요, 좋습니다"},
            "1_thumb-up":   {"move": ["hello"],  "say": "네 좋아요. 최고입니다.!"},
            "1_thumb-down": {"move": ["sit"],    "say": "네, 알겠습니다. 아쉽네요!"},
            "2_thumb-up":   {"move": ["hello"],  "say": "네 좋아요. 최고입니다.!"},
            "2_thumb-down": {"move": ["sit"],    "say": "네, 알겠습니다. 아쉽네요!"},
            "1_victory":    {"move": ["hello"],  "say": "네 좋아요! 잘 하셨어요!"},
            "2_victory":    {"move": ["hello"],  "say": "네 좋아요! 잘 하셨어요!"},
            "1_OK":         {"move": ["hello"],  "say": "네 알겠습니다. 아무 문제 없군요"},
            "finger-heart": {"move": ["heart_"], "say": "저도 사랑해요. 멋진 하루 되세요!"},
            "help!":        {"move": ["scrape"], "say": "위험발견! 도와주세요! 위험 상황입니다"},
            "test1":        {"move": ["sit"],    "say": "시험1"},
            "test2":        {"move": ["stand"],  "say": "시험2"},
        }

        # ===== Greengrass IPC 클라이언트 =====
        self.ipc = GreengrassCoreIPCClientV2()

        # IPC 구독 핸들(제스처 on/off 관리용)
        self._ipc_main_sub = None
        self._ipc_gesture_sub = None

        # 메인 명령 토픽 구독 시작
        self._start_ipc_main_subscription()
        logger.info("[IPC] subscribe_to_iot_core (main) started")

        # 워커
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    # =========================
    # IPC Subscribe: Main topic
    # =========================
    def _start_ipc_main_subscription(self):
        def on_stream_event(event):
            try:
                topic = event.message.topic_name
                payload_bytes = event.message.payload or b""
                payload_str = payload_bytes.decode("utf-8", errors="ignore")
                logger.info(f"[IPC<-IoT] {topic}: {payload_str}")
                payload = json.loads(payload_str) if payload_str else {}
                self._handle_main_payload(payload)
            except Exception as e:
                logger.exception(f"on_stream_event error: {e}")

        def on_stream_error(error):
            logger.error(f"[IPC (main)] stream error: {error}")
            return False

        def on_stream_closed():
            logger.warning("[IPC (main)] stream closed")

        if self._ipc_main_sub:
            try: self._ipc_main_sub.close()
            except Exception: pass
            self._ipc_main_sub = None

        _, self._ipc_main_sub = self.ipc.subscribe_to_iot_core(
            topic_name=MQTT_CMD_TOPIC, qos=QOS.AT_LEAST_ONCE,
            on_stream_event=on_stream_event, on_stream_error=on_stream_error, on_stream_closed=on_stream_closed
        )

    # ==================================
    # IPC Subscribe: Gesture topic on/off
    # ==================================
    def _start_gesture_subscription(self):
        self.gesture_switch = True
        if self._ipc_gesture_sub:
            logger.warning("Gesture IPC subscription already running.")
            return

        def on_stream_event(event):
            try:
                payload_bytes = event.message.payload or b""
                payload_str = payload_bytes.decode("utf-8", errors="ignore")
                logger.info(f"[IPC<-IoT] {MQTT_CMD_GESTURE}: {payload_str}")
                payload = json.loads(payload_str) if payload_str else {}
                self._handle_gesture_payload(payload)
            except Exception as e:
                logger.exception(f"gesture on_stream_event error: {e}")

        def on_stream_error(error):
            logger.error(f"[IPC (gesture)] stream error: {error}")
            return False

        def on_stream_closed():
            logger.warning("[IPC (gesture)] stream closed")

        _, self._ipc_gesture_sub = self.ipc.subscribe_to_iot_core(
            topic_name=MQTT_CMD_GESTURE, qos=QOS.AT_LEAST_ONCE,
            on_stream_event=on_stream_event, on_stream_error=on_stream_error, on_stream_closed=on_stream_closed
        )
        logger.info("[IPC] gesture subscription started")

    def _stop_gesture_subscription(self):
        self.gesture_switch = False
        if not self._ipc_gesture_sub: return
        try: self._ipc_gesture_sub.close()
        except Exception as e: logger.error(f"Error while closing gesture subscription: {e}")
        finally:
            self._ipc_gesture_sub = None
            logger.info("[IPC] gesture subscription stopped")

    # =========================
    # Main payload handler (IPC)
    # =========================
    def _handle_main_payload(self, payload: dict):
        if "command" in payload:
            cmd_list = payload.get("command", [])
            if isinstance(cmd_list, list):
                for cmd in cmd_list:
                    if cmd == "gesture_on": self._start_gesture_subscription()
                    elif cmd == "gesture_off": self._stop_gesture_subscription()
                    elif cmd == "speaker_on": self.saying_switch = True
                    elif cmd == "speaker_off": self.saying_switch = False; self.say_ = None
                    elif cmd == "safe_mode_on": self.safe_mode = True; self.custom_move_speed = 0.11
                    elif cmd == "safe_mode_off": self.custom_move_speed = LIN_SPEED; self.safe_mode = False
                    elif cmd == 'set_move_duration_up': self.custom_move_duration = min(5.0, self.custom_move_duration + 0.5)
                    elif cmd == 'set_move_duration_down': self.custom_move_duration = max(0.5, self.custom_move_duration - 0.5)
                    elif cmd == 'get_status': logger.info(f"Status - is_sitting: {self.is_sitting}, safe_mode: {self.safe_mode}, custom_move_speed: {self.custom_move_speed}, custom_move_duration: {self.custom_move_duration}, saying_switch: {self.saying_switch}")

        if "say" in payload and self.saying_switch:
            say = payload.get("say")
            if isinstance(say, str) and say.strip(): self.say_ = say

        seq = self._parse_payload_to_sequence(payload)
        if not seq:
            if "move" in payload or ("command" in payload and not all(c in ["gesture_on", "gesture_off", "speaker_on", "speaker_off", "safe_mode_on", "safe_mode_off", "set_move_duration_up", "set_move_duration_down", "get_status"] for c in payload["command"])):
                logger.error(f"bad cmd payload: {payload}")
            return

        # ===== [핵심 수정 1/2] =====
        with self.lock:
            # 새로운 명령을 받으면, pending 큐를 완전히 비우고 새 명령만 추가한다.
            # 이렇게 하면 여러 인터럽트 명령이 쌓이는 것을 방지하고 최신 명령만 남긴다.
            self.pending.clear()
            self.pending.append(list(seq))
            logger.info(f"New command queued, clearing previous pending commands: {list(seq)}")
            
            # 만약 시퀀스가 실행 중이라면, 인터럽트를 건다.
            if self.seq_running:
                if not self.interrupt.is_set():
                    logger.warning("Interrupting current sequence for new command.")
                    self.interrupt_reason = "interrupted_by_new_command"
                    self.interrupt.set()
        # ==========================
        
    # ===========================
    # Gesture payload handler IPC
    # ===========================
    def _handle_gesture_payload(self, payload: dict):
        if not self.gesture_switch: return
        
        with self.lock:
            if self.seq_running or self.pending:
                logger.warning("Robot is busy, ignoring gesture command.")
                return

        cls_gesture_list = payload.get('results', [])
        if not cls_gesture_list: return
        cls_gesture = cls_gesture_list[0].get('class')
        if not cls_gesture: return
            
        action_data = self.GESTURE_ACTION_MAP.get(cls_gesture)
        if not action_data:
            logger.info(f"Gesture '{cls_gesture}' received, but no action is mapped.")
            return

        self._stop_gesture_subscription()
        
        with self.lock:
            if self.saying_switch: self.say_ = action_data["say"]
            sequence = action_data["move"]
            # [수정] 제스처 명령도 pending 큐에 추가
            self.pending.append(sequence)
            logger.info(f"Gesture action queued: {sequence}")
        
        try:
            self.ipc.publish_to_iot_core(
                topic_name=MQTT_GESTURE_TOPIC, qos=QOS.AT_LEAST_ONCE,
                payload=json.dumps(payload).encode("utf-8")
            )
            logger.info(f"[GESTURE RESULT] -> {MQTT_GESTURE_TOPIC}: {payload}")
        except Exception as e:
            logger.error(f"[IPC] publish error on gesture result: {e}")
        
    # =========================
    # 워커 / 시퀀스 실행 루프
    # =========================
    def _worker_loop(self):
        while True:
            # ===== [핵심 수정 2/2] =====
            # 루프의 시작에서 상태를 확인하고 결정
            with self.lock:
                # 1. 실행할 준비가 되었고, 대기중인 명령이 있는가?
                if not self.seq_running and self.pending:
                    nxt = self.pending.popleft()
                    self._start_sequence_unlocked(nxt)
                # 2. 실행 중인 것이 없다면, 할 일이 없으므로 루프 계속
                elif not self.seq_running:
                    time.sleep(0.05) # CPU 사용량 감소
                    continue
                # 3. 실행 중이라면, 루프 아래로 내려가서 _execute_sequence 실행
            
            # lock을 잠시 풀고, 시간이 오래 걸리는 _execute_sequence 실행
            current_seq_id = self.seq_id
            current_sequence_list = list(self.current_sequence)
            result, reason = self._execute_sequence(current_seq_id, current_sequence_list)

            # 실행이 끝나면 다시 lock을 잡고 상태 정리
            with self.lock:
                # 인터럽트 등으로 인해 _worker_loop가 도는 사이에 새 시퀀스가 시작되었을 수 있다.
                # 방금 실행이 끝난 시퀀스가 현재 시퀀스 ID와 일치할 때만 상태를 정리한다.
                if self.seq_id == current_seq_id:
                    self._publish_result(
                        result=result, reason=reason, seq_id=current_seq_id,
                        sequence=self.current_sequence, conducted=self.conducted,
                        error_offset=self._finalize_error_offset(self.current_sequence, self.conducted, self.error_offset, result),
                    )
                    # 상태 초기화
                    self.seq_running = False
                    self.current_sequence = []
                    self.conducted = []
                    self.error_offset = []
                    self.interrupt.clear()
                    self.interrupt_reason = None
                    logger.info(f"[SEQ {current_seq_id}] finished and cleaned up.")
            # ==========================

    def _start_sequence_unlocked(self, seq):
        self.seq_id += 1
        self.current_sequence = list(seq)
        self.conducted = []
        self.error_offset = []
        self.seq_running = True
        logger.info(f"[SEQ {self.seq_id}] accepted: {self.current_sequence}")

    # =========================
    # 시퀀스 실행 + TTS
    # =========================
    def _execute_sequence(self, seq_id, sequence):
        def _synthesize_speech(speed, text, langCode, voiceId):
            polly = boto3.client('polly')
            ssml_text = f'<speak><prosody rate=\"{speed}%\">{text}</prosody></speak>'
            resp = polly.synthesize_speech(
                Text=ssml_text, TextType='ssml', Engine='neural',
                LanguageCode=langCode, OutputFormat='mp3', VoiceId=voiceId
            )
            encoded = base64.b64encode(resp['AudioStream'].read()).decode()
            with open('/home/unitree/temp.mp3', 'wb') as f:
                f.write(base64.b64decode(encoded))

        logger.info(f"[SEQ {seq_id}] start")
        
        if self.say_ is not None and self.saying_switch:
            TTS_WAIT_TIMEOUT = 7.0 # TTS 재생을 최대 5초까지 기다림
            try:
                speed, text, langCode, voiceId = 100, "멍멍...." + self.say_, 'ko-KR', 'Jihye'
                _synthesize_speech(speed, text, langCode, voiceId)
                audio_command = ["runuser", "-u", "unitree", "--", "mpg123", "-o", "pulse", "/home/unitree/temp.mp3"]
                result = subprocess.run(audio_command, capture_output=True, text=True, 
                                        timeout=TTS_WAIT_TIMEOUT)
                if result.returncode == 0: logger.info("TTS playback OK")
                else: logger.error(f"TTS playback failed: {result.stderr}")
            except subprocess.TimeoutExpired:
                logger.warning(f"TTS playback timed out after {TTS_WAIT_TIMEOUT} seconds. Terminating audio and continuing sequence.")

            finally:
                self.say_ = None

        for op in sequence:
            if self.interrupt.is_set():
                logger.warning(f"[SEQ {seq_id}] interrupted before op='{op}'")
                self._emergency_brake()
                with self.lock:
                    remain = len(sequence) - len(self.conducted)
                    self.error_offset.extend([True] * max(0, remain))
                return False, (self.interrupt_reason or "interrupted")

            ok, stop_requested, err = self._do_op(op)

            with self.lock:
                self.conducted.append(op)
                self.error_offset.append(not ok)

            if stop_requested:
                logger.warning(f"[SEQ {seq_id}] stopped_by_user at op='{op}'")
                self._emergency_brake()
                with self.lock:
                    remain = len(sequence) - len(self.conducted)
                    self.error_offset.extend([True] * max(0, remain))
                return False, "stopped_by_user"

            if not ok:
                logger.error(f"[SEQ {seq_id}] op error '{op}': {err}")
                self._emergency_brake()
                with self.lock:
                    remain = len(sequence) - len(self.conducted)
                    self.error_offset.extend([True] * max(0, remain))
                return False, f"op_error: {err}"

        logger.info(f"[SEQ {seq_id}] complete")
        return True, None

    # =========================
    # 로봇 동작들
    # =========================
    def _check_sdk_return(self, ret, command_name="command"):
        if ret == 0 or ret is None: return True, None
        else:
            error_msg = f"{command_name}_failed_with_code_{ret}"; logger.error(error_msg)
            return False, error_msg

    def _interruptible_sleep(self, duration):
        start = time.time()
        while time.time() - start < duration:
            if self.interrupt.is_set(): return False
            time.sleep(0.05)
        return True

    def _wake_up_locomotion(self):
        logger.info("Waking up locomotion mode...")
        self.bot.Move(0, 0, 0)
        if not self._interruptible_sleep(POST_STAND_WAKE_UP_DURATION):
            return False, "interrupted_during_wake_up"
        self.bot.StopMove()
        return True, None

    def _do_op(self, op):
        try:
            if self.is_sitting and op not in ["sit", "stand", "stop"]:
                logger.warning(f"Robot is sitting. Standing up before executing '{op}'.")
                stand_ok, _, stand_reason = self._do_op("stand")
                if not stand_ok: return False, False, f"auto_stand_failed: {stand_reason}"

            if op in self.CUSTOM_OPERATION_LIST:
                for cus_op in self.CUSTOM_OPERATION_LIST[op]:
                    ok, stop_requested, reason = self._do_op(cus_op)
                    if not ok or stop_requested: return ok, stop_requested, reason
                if op == self.gesture_on_area_move_command or op == self.gesture_on_area_test:
                    logger.info("gesture mode - start"); self._start_gesture_subscription()
                elif op in self.othermove_gesture: self._stop_gesture_subscription()
                return True, False, None

            if op == "sit":
                ok, reason = self._check_sdk_return(self.bot.StandDown(), "StandDown")
                if ok: self.is_sitting = True
                return ok, False, reason
            if op == "stand":
                ok, reason = self._check_sdk_return(self.bot.StandUp(), "StandUp")
                if ok:
                    self.is_sitting = False; wake_ok, wake_reason = self._wake_up_locomotion()
                    if not wake_ok: return False, False, wake_reason
                return ok, False, reason
            if op == "recovery_stand":
                ok, reason = self._check_sdk_return(self.bot.RecoveryStand(), "RecoveryStand")
                if ok:
                    self.is_sitting = False; wake_ok, wake_reason = self._wake_up_locomotion()
                    if not wake_ok: return False, False, wake_reason
                return ok, False, reason

            if op == "forward": return self._do_move(+self.custom_move_speed, 0.0, 0.0, self.custom_move_duration)
            if op == "backward": return self._do_move(-self.custom_move_speed, 0.0, 0.0, self.custom_move_duration)
            if op == "left": return self._do_move(0.0, +self.custom_move_speed, 0.0, self.custom_move_duration)
            if op == "right": return self._do_move(0.0, -self.custom_move_speed, 0.0, self.custom_move_duration)
            if op == "turn_left": return self._do_move(0.0, 0.0, +YAW_SPEED, TURN_DURATION)
            if op == "turn_right": return self._do_move(0.0, 0.0, -YAW_SPEED, TURN_DURATION)
            if op == "turn_left_S": return self._do_move(0.0, 0.0, +YAW_SPEED, TURN_DURATION * 2/3)
            if op == "turn_right_S": return self._do_move(0.0, 0.0, -YAW_SPEED, TURN_DURATION * 2/3)
            if op == "forward_L": return self._do_move(+self.custom_move_speed, 0.0, 0.0, self.custom_move_duration * FORWARD_L)
            if op == "forward_S": return self._do_move(+self.custom_move_speed, 0.0, 0.0, self.custom_move_duration * FORWARD_S)
            
            if op == "damp": return self._check_sdk_return(self.bot.Damp(), "Damp") + (False,)
            if op == "balance_stand": return self._check_sdk_return(self.bot.BalanceStand(), "BalanceStand") + (False,)
            if op == "scrape": return self._check_sdk_return(self.bot.Scrape(), "Scrape") + (False,)
            if op == "hello": return self._check_sdk_return(self.bot.Hello(), "Hello") + (False,)
            if op == "stretch": return self._check_sdk_return(self.bot.Stretch(), "Stretch") + (False,)
            if op == "dance1": return self._check_sdk_return(self.bot.Dance1(), "Dance1") + (False,)
            if op == "dance2": return self._check_sdk_return(self.bot.Dance2(), "Dance2") + (False,)
            if op == "heart_": return self._check_sdk_return(self.bot.Heart(), "Heart") + (False,)
            
            if op == "stop": return False, True, "stopped_by_user"
            return False, False, f"unknown_op:{op}"
        except Exception as e:
            logger.error(f"Exception during op '{op}': {e}")
            return False, False, str(e)

    def _do_move(self, vx, vy, yaw, duration):
        try:
            start_time = time.time()
            interrupted = False
            while time.time() - start_time < duration:
                if self.interrupt.is_set():
                    interrupted = True
                    logger.warning("Move command interrupted during execution.")
                    break
                self.bot.Move(vx, vy, yaw)
                time.sleep(0.05)
            self.bot.StopMove()
            if interrupted: return False, False, "interrupted"
            return True, False, None
        except Exception as e:
            self._emergency_brake()
            return False, False, str(e)

    def _emergency_brake(self):
        for _ in range(EMERGENCY_BRAKE_PULSES):
            try: self.bot.StopMove()
            except Exception: pass
            time.sleep(EMERGENCY_BRAKE_INTERVAL)

    def _parse_payload_to_sequence(self, payload):
        if "move" in payload: seq = payload["move"]
        elif "command" in payload: seq = payload["command"]
        else: return None
        return seq if isinstance(seq, list) and all(isinstance(x, str) for x in seq) else None

    def _finalize_error_offset(self, sequence, conducted, error_offset, result_ok):
        if result_ok: return [False] * len(sequence)
        else:
            pad = len(sequence) - len(conducted)
            return list(error_offset) + [True] * max(0, pad)

    def _calc_remaining(self, sequence, conducted):
        remaining, conducted_copy = [], list(conducted)
        for op in sequence:
            try: conducted_copy.remove(op)
            except ValueError: remaining.append(op)
        return remaining

    def _publish_result(self, result, reason, seq_id, sequence, conducted, error_offset):
        payload = { "result": bool(result), "seq": sequence, "conducted": conducted }
        try:
            self.ipc.publish_to_iot_core(
                topic_name=MQTT_RESULT_TOPIC, qos=QOS.AT_LEAST_ONCE,
                payload=json.dumps(payload).encode("utf-8")
            )
            logger.info(f"[RESULT] -> {MQTT_RESULT_TOPIC}: {payload}")
        except Exception as e:
            logger.error(f"[IPC] publish error: {e}")

    def shutdown(self):
        logger.info("Shutdown requested")
        self._stop_gesture_subscription()
        with self.lock:
            if self.seq_running:
                self.interrupt_reason = "shutdown"
                self.interrupt.set()
        time.sleep(0.3)
        try: self._emergency_brake()
        except Exception: pass

def main():
    if len(sys.argv) > 1: ChannelFactoryInitialize(0, sys.argv[1])
    else: ChannelFactoryInitialize(0)
    ctl = RobotController()
    def _sig(_s, _f):
        print("\n[EXIT] emergency stop...")
        try: ctl.shutdown()
        finally: sys.exit(0)
    signal.signal(signal.SIGINT, _sig)
    while True:
        time.sleep(1)

if __name__ == "__main__":
    print("WARNING: Please ensure there are no obstacles around the robot while running this program.")
    main()