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
