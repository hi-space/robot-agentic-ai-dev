#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import time
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy

from unitree_go.msg import LowState          # /lowstate message
from awsiot.greengrasscoreipc.clientv2 import GreengrassCoreIPCClientV2
from awsiot.greengrasscoreipc.model import QOS

ROS_TOPIC = "/lowstate"
IOT_TOPIC = "robot/state/battery"
QOS_LEVEL = QOS.AT_LEAST_ONCE
qos_profile = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
MIN_PERIOD = 5.0                  

class BatteryToIoTPublisher(Node):
    def __init__(self):
        super().__init__("battery_to_iot_publisher")
        self.ipc = GreengrassCoreIPCClientV2()

        self.last_soc = None
        self.last_pub_ts = 0.0

        self.create_subscription(LowState, ROS_TOPIC, self.cb, qos_profile)
        self.check_topic_status()
        self.get_logger().info(
            f"[PUB] ROS2 '/lowstate' → IoT Core '{IOT_TOPIC}' 브리지 시작"
        )

    def cb(self, msg: LowState):
        try:
            self.get_logger().info("[CB] /lowstate message arrived")
            now = time.time()
            soc = float(msg.bms_state.soc)          # Battery Level(%) (0-100)
            v   = float(msg.power_v) if hasattr(msg, "power_v") else None
            a   = float(msg.power_a) if hasattr(msg, "power_a") else None

            # If changed or minimum period has elapsed, send
            if self.last_soc is None or self.last_soc != soc or (now - self.last_pub_ts) >= MIN_PERIOD:
                payload = {
                    "battery": soc,                 # Battery Information
                    "timestamp": int(now)
                }

                self.ipc.publish_to_iot_core(
                    topic_name=IOT_TOPIC,
                    qos=QOS_LEVEL,
                    payload=json.dumps(payload).encode("utf-8")
                )
                self.last_soc = soc
                self.last_pub_ts = now
                self.get_logger().info(f"[PUB] → IoT {IOT_TOPIC}: {payload}")

        except Exception as e:
            self.get_logger().error(f"publish 실패: {e}")

    def check_topic_status(self):

        """Check ROS2 topic status"""
        topic_names = self.get_topic_names_and_types()
        lowstate_exists = any(ROS_TOPIC in topic for topic, _ in topic_names)
    
        if lowstate_exists:
            self.get_logger().info(f"[DEBUG] {ROS_TOPIC} 토픽 확인됨")
        else:
            self.get_logger().warn(f"[DEBUG] {ROS_TOPIC} 토픽을 찾을 수 없음. 사용 가능한 토픽들:")
            for topic, types in topic_names:
                if 'lowstate' in topic.lower() or 'battery' in topic.lower():
                    self.get_logger().info(f"  - {topic}: {types}")

def main():
    rclpy.init()
    node = BatteryToIoTPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == "__main__":
    main()