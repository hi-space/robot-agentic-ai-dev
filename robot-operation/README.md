To run the webrtc, please see the detailed instruction [here](https://github.com/aws-samples/python-samples-for-amazon-kinesis-video-streams-with-webrtc).

For robot_kvsWebRTCClientMaster.py , you have to match the proper video input device. 

Simple usage
```
python3 ./robot_kvsWebRTCClientMaster.py --channel-arn [channel_arm]
```



------


To operate the Unitree Go2 using AWS Greengrass, add and deploy the following components:


File	Description
- battery_publisher.py: Publishes the robot’s battery status to an MQTT topic.
- control_subscriber.py: Subscribes to the MQTT topic and executes robot control commands.

These components enable bidirectional communication between the robot and the AWS IoT Core, supporting real-time monitoring and remote operation.

Each component used by AWS Greengrass must include its artifacts — the actual executable files, scripts, or configuration resources that Greengrass will deploy to the device.

**Recommended References**
We recommend referring to the following resources to better understand and utilize this project:

- [**AWS Greengrass Workshop**](https://catalog.workshops.aws/) → *Greengrass section*  
  Learn how to configure AWS Greengrass, manage components, and deploy artifacts to IoT devices.

- [**Unitree Go2 ROS Setup Guide**](https://docs.ros.org/en/humble/Installation.html)  
  Provides instructions for setting up the ROS environment used by Unitree Go2 for robot control and sensor communication.

- [**Unitree Go2 Python SDK**](https://github.com/unitreerobotics/unitree_sdk2_python)  
  Provides the Unitree Go2 Python SDK. We developed the robot operation code based on this SDK.
