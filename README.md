# Agentic RoboDog 

COEX seoul에서 10월 14-16까지 진행되는 AWS Industry Week에서 진행되는 Agentic RoboDog에 대한 Repository입니다. 주요 Architecture는 아래를 참조하세요.

<img width="800" alt="architecture" src="https://github.com/user-attachments/assets/69446a72-437b-4f60-9b9f-941d93aa8585" />



## Robot AgentCore Gateway

여기서는 Robot의 제어 명령어 전달을 위해 MCP를 이용합니다. 이를 구현하여 배포할 때에는 AgnetCore Gateway와 Lambda를 이용합니다. 

### MCP Interface Tool Spec

Robot 제어를 위한 명령(command)는 action과 message로 주어집니다. 이는 아래와 같이 string으로 주어지고 action은 구체적인 예제를 가지고 있습니다. 아래에서는 Robot의 기분을 나타내는 HAPPY, NEUTRAL, SAD, ANGRY의 action을 가지고 있습니다. 

```java
{
    "name": "command",
    "description": "당신은 로봇 컨트롤러입니다. 로봇을 컨트롤하기 위한 명령은 action과 message입니다. 적절한 로봇의 동작명을 action으로 전달하고, 로봇이 전달할 메시지를 message로 전달하세요. action은 HAPPY, NEUTRAL, SAD, ANGRY중 하나를 선택합니다.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string"
            },
            "message": {
                "type": "string"
            }
        },
        "required": ["action"]
    }
}
```

### MCP 서버의 동작

[lambda_function.py](./gateway/mcp-interface/lambda-mcp-interface-for-robo/lambda_function.py)와 같이 수신된 event로부터 지원하는 tool인지를 toolName으로 확인한 후에 action과 message를 추출하여 활용합니다.

```python
def lambda_handler(event, context):
    toolName = context.client_context.custom['bedrockAgentCoreToolName']
    
    delimiter = "___"
    if delimiter in toolName:
        toolName = toolName[toolName.index(delimiter) + len(delimiter):]

    action = event.get('action')
    message = event.get('message')

    if toolName == 'command':
        result = command_robot(action, message)
        return {
            'statusCode': 200, 
            'body': result
        }
```

### MCP Gateway의 생성

[create_gateway_tool.py](./gateway/mcp-interface/create_gateway_tool.py)와 같이 MCP Gateway를 생성합니다.

```pyhton
gateway_id = config.get('gateway_id')    
gateway_url = f'https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp'
agentcore_gateway_iam_role = config['agentcore_gateway_iam_role']
auth_config = {
    "customJWTAuthorizer": { 
        "allowedClients": [client_id],  
        "discoveryUrl": cognito_discovery_url
    }
}
response = gateway_client.create_gateway(
    name=gateway_name,
    roleArn = agentcore_gateway_iam_role,
    protocolType='MCP',
    authorizerType='CUSTOM_JWT',
    authorizerConfiguration=auth_config, 
    description=f'AgentCore Gateway for {projectName}'
)
```

이제 아래와 같이 target을 gateway에 deploy합니다.

```python
TOOL_SPEC = json.load(open(os.path.join(script_dir, "tool_spec.json")))
lambda_target_config = {
    "mcp": {
        "lambda": {
            "lambdaArn": lambda_function_arn, 
            "toolSchema": {
                "inlinePayload": [TOOL_SPEC]
            }
        }
    }
}
credential_config = [ 
    {
        "credentialProviderType" : "GATEWAY_IAM_ROLE"
    }
]
response = gateway_client.create_gateway_target(
    gatewayIdentifier=gateway_id,
    name=targetname,
    description=f'{targetname} for {projectName}',
    targetConfiguration=lambda_target_config,
    credentialProviderConfigurations=credential_config)

target_id = response["targetId"]
```


### MCP 서버 설치

[create_gateway_role.py](./gateway/mcp-interface/create_gateway_role.py)을 이용해 필요한 Role을 생성합니다.

```text
python create_gateway_role.py
```
[create_gateway_tool.py](./gateway/mcp-interface/create_gateway_tool.py)을 이용해 gateway와 target을 설치합니다. 이때 target의 실행을 위해 lambda도 설치합니다.

```text
python create_gateway_tool.py
```

### MCP 서버의 활용

[test_mcp_remote.py](./gateway/mcp-interface/test_mcp_remote.py)와 같이 활용합니다.

```python
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client(mcp_url, headers, timeout=120, terminate_on_close=False) as (
    read_stream, write_stream, _):

    async with ClientSession(read_stream, write_stream) as session:
        tool_result = await asyncio.wait_for(session.list_tools(), timeout=60)
        for tool in tool_result.tools:
            print(f"  - {tool.name}: {tool.description[:100]}...")

        targret_name = config['target_name']
        tool_name = f"{targret_name}___command"
        result = await asyncio.wait_for(session.call_tool(tool_name, params), timeout=30)
```

생성된 MCP의 정보는 아래와 같이 가져옵니다.

```python
gateway_url = f'https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp'
bearer_token = retrieve_bearer_token(config['secret_name'])

return {
    "mcpServers": {
        "agentcore-gateway": {
            "type": "streamable_http",
            "url": gateway_url,
            "headers": {
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            }
        }
    }
}
```


## Robot의 Feedback

Robot에서 지정된 topic (robo/feedback)으로 feedback에 대한 메시지를 전송하면 IoT Core를 통해 [Lambda](./feedback-manager/lambda-feedback-manager-for-robo/lambda_function.py)에서 수신합니다. 이 메시지는 SQS (fifo)에 순차적으로 기록되면, 이후 client에서 가져다가 활용합니다. 

### 상세 구현

Robot의 Feedback을 위해서는 IoT Core의 topic을 수신하기 위한 SQS, Rule과 Lambda가 필요합니다. 상세코드는 [create_feedback_manager.py](./feedback-manager/create_feedback_manager.py)을 참조합니다.

SQS를 생성합니다.

```python
sqs_client = boto3.client('sqs', region_name=region)
fifo_queue_name = queue_name if queue_name.endswith('.fifo') else f"{queue_name}.fifo"

response = sqs_client.create_queue(
    QueueName=fifo_queue_name,
    Attributes={
        'VisibilityTimeout': '30',
        'MessageRetentionPeriod': '1209600',  # 14 days
        'FifoQueue': 'true',
        'ContentBasedDeduplication': 'true'  # Enable content-based deduplication
    }
)
```

Rule을 생성합니다.

```python
iot_client = boto3.client('iot', region_name=region)
sql_statement = f"SELECT * FROM '{topic_filter}'"

lambda_action = {
    'lambda': {
        'functionArn': lambda_function_arn
    }
}
iot_client.replace_topic_rule(
    ruleName=rule_name,
    topicRulePayload={
        'sql': sql_statement,
        'actions': [lambda_action],
        'ruleDisabled': False,
        'description': f'IoT rule to trigger Lambda for {topic_filter} topic'
    }
)
```

Lambda를 생성합니다.

```python
lambda_function_name = 'lambda-' + current_folder_name + '-for-' + config['projectName']

with zipfile.ZipFile(lambda_function_zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:            
  for root, dirs, files in os.walk(lambda_dir):
      for file in files:
          file_path = os.path.join(root, file)
          arcname = os.path.relpath(file_path, lambda_dir)
          zip_file.write(file_path, arcname)

response = lambda_client.create_function(
    FunctionName=lambda_function_name,
    Runtime='python3.13',
    Handler='lambda_function.lambda_handler',
    Role=lambda_function_role,
    Description=f'Lambda function for {lambda_function_name}',
    Timeout=60,
    Code={
        'ZipFile': open(lambda_function_zip_path, 'rb').read()
    }
)
lambda_function_arn = response['FunctionArn']
```

여기서 구현한 Lambda는 event는 SQS에 push 하는 역할을 수행합니다. 상세코드는 [lambda_function.py](./feedback-manager/lambda-feedback-manager-for-robo/lambda_function.py)을 참조합니다.

```python
def lambda_handler(event, context):
    sqs = boto3.client('sqs')
    region = os.environ.get('AWS_REGION', 'us-west-2')
    account_id = context.invoked_function_arn.split(':')[4]
    
    queue_url = f"https://sqs.{region}.amazonaws.com/{account_id}/robo_feedback.fifo"
        
    response = sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(event),
        MessageGroupId='robo-feedback-group',  # Required for FIFO queue
        MessageDeduplicationId=str(context.aws_request_id),  # Required for FIFO queue
        MessageAttributes={
            'source': {
                'StringValue': 'iot-core',
                'DataType': 'String'
            },
            'timestamp': {
                'StringValue': str(context.aws_request_id),
                'DataType': 'String'
            }
        }
    )
        
    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'Event successfully pushed to SQS',
            'messageId': response['MessageId'],
            'event': event
        })
    }
```

Client에서 SQS에 저장된 Robot의 Feedback은 아래와 같이 가져옵니다.

```python
sqs = boto3.client('sqs', region_name=region)
queue_url = f"https://sqs.{region}.amazonaws.com/{account_id}/robo_feedback.fifo"
sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=['All'])
while True:
    response = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=20,  # Long polling
        MessageAttributeNames=['All']
    )
    messages = response.get('Messages', [])
    if messages:
        for message in messages:
            message_body = json.loads(message['Body'])
                
            # Delete message after processing
            sqs.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=message['ReceiptHandle']
            )
```

### 설치 및 테스트

아래 명령어로 설치합니다.

```text
python create_feedback_manager.py
```

[test_feedback.py](./feedback-manager/test_feedback.py)를 이용해 테스트 할 수 있습니다.

```text
python test_feedback.py
```

이후 MQTT Tester를 이용해 메시지 전송후 결과를 확인하면 아래와 같습니다.

```text
python test_feedback.py
Reading messages from SQS queue: https://sqs.ap-northeast-2.amazonaws.com/533267442321/robo_feedback.fifo
Press Ctrl+C to stop...
Queue access verified successfully
[2025-09-08 22:27:59.012100] Received 1 message(s):
Message ID: 37211858-c574-4c64-9946-f5b02731209e
Receipt Handle: AQEB+UZkhd15KP6vBrklq9gkTVsQE4G/ahxVrupnVyDPPT/4neGpqHARMSk1SFNp1xXdYwqrD1gdHDzlfZY15xkqn87QjF3rFrM5bVPHeTLFAJPwV2QyssUJnAQLjaywZBfGENCqp/l191c/tUF1BAmfnBI9Kj/8bm5r5Da01m5CjxAyy7qAok5FQZnrNqQTFl/0cZtTTupEw4LDIPHZwGAysd08XwkTdMMGK0rsP6B47gvYxVQsZdrh8g+VkOMQkMgMirb70gUTyblPNRIVPjrymNFsTQ+AvwzsUwaTHvNgktA=
Message Attributes:
  source: iot-core
  timestamp: 4a19c080-9f35-4a89-881d-af0919db9998
Message Body: {
  "message": "Hello 1"
}
```


## Robot Text to Speech

[robo-polly.py](./robo-polly/robo-polly.py)와 같이 polly를 이용해 text를 speech로 변환합니다. 아래와 같이 출력 포맷으로 mp3, ogg등을 선택할 수 있습니다. 음성은 voiceId로 설정하는데, 'Seoyeon' 또는 'Jihye'를 사용할 수 있습니다.

```python
polly_client = boto3.client('polly')
ssml_text = f'<speak><prosody rate="{speed}%">{text}</prosody></speak>'
response = polly_client.synthesize_speech(
    Text=ssml_text,
    TextType='ssml', 
    Engine='neural', 
    LanguageCode=langCode, 
    OutputFormat='mp3', # 'json'|'mp3'|'ogg_vorbis'|'pcm',
    VoiceId=voiceId
)
```

## Robot Direct Controller

Robot 제어는 기본적으로 Agent에 의해 진행이되나, 직접 제어가 필요한 경우를 대비하여 아래와 같은 lambda를 구현합니다. [robot controller](./robo-controller/lambda-robo-controller-for-robo/lambda_function.py)는 lambda를 이용해 action과 message를 설정합니다. 이때 client에서 trigger할 때에는 아래와 같이 수행합니다. 

```python
def test_robo_controller(lambda_function_name, action, message):
    payload = {
        'action': action,
        'message': message
    }
    print(f"payload: {payload}")

    lambda_client = boto3.client(
        service_name='lambda',
        region_name=region,
    )

    output = lambda_client.invoke(
        FunctionName=lambda_function_name,
        Payload=json.dumps(payload),
    )
    print(f"output: {output}")

action = 'HAPPY'
message = '오늘은 정말 멋지네요!'
test_robo_controller(lambda_function_name, action, message)
```

이때의 실행 결과는 아래와 같습니다.

<img width="264" height="278" alt="image" src="https://github.com/user-attachments/assets/0a0f41e6-ebe6-403f-9cdb-153dd3afa57d" />

