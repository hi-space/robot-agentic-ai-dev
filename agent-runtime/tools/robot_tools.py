from strands import tool
from datetime import datetime
import json
import boto3
import os
import time
from typing import Optional, List, Dict, Any
from utils.s3_util import download_image_from_s3


def _add_presigned_urls_to_messages(result: Dict[str, Any]) -> Dict[str, Any]:
    """Helper function to add presigned URLs to messages containing S3 paths.
    
    Args:
        result: Dictionary containing messages with S3 file paths
        
    Returns:
        Updated dictionary with presigned URLs added to messages
    """
    if "messages" not in result:
        return result
    
    try:
        s3_client = boto3.client('s3', region_name='ap-northeast-2')
        
        for msg in result["messages"]:
            if "filename" in msg and msg["filename"].startswith("s3://"):
                try:
                    # Extract bucket and key from S3 path
                    s3_path = msg["filename"].replace("s3://", "")
                    parts = s3_path.split("/", 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        # Generate presigned URL (valid for 1 hour)
                        presigned_url = s3_client.generate_presigned_url(
                            'get_object',
                            Params={'Bucket': bucket, 'Key': key},
                            ExpiresIn=3600
                        )
                        msg["image_url"] = presigned_url
                except Exception as e:
                    print(f"Error generating presigned URL for {msg['filename']}: {e}")
                    # Continue without image URL if generation fails
    except Exception as e:
        print(f"Error creating S3 client: {e}")
    
    return result


def _clear_queue(queue_name: str, config: dict, sqs_client=None) -> None:
    """Helper function to clear all messages from SQS FIFO queue.
    
    Args:
        queue_name: Name of the FIFO queue (without .fifo suffix)
        config: Configuration dictionary containing accountId
        sqs_client: Optional SQS client (creates new one if not provided)
    """
    try:
        region = "ap-northeast-2"
        account_id = config['accountId']
        
        if sqs_client is None:
            sqs_client = boto3.client('sqs', region_name=region)
        
        queue_url = f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}.fifo"
        
        # Clear all messages in the queue
        while True:
            response = sqs_client.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=0
            )
            messages = response.get('Messages', [])
            if not messages:
                break
            
            # Delete all messages
            for message in messages:
                try:
                    sqs_client.delete_message(
                        QueueUrl=queue_url,
                        ReceiptHandle=message['ReceiptHandle']
                    )
                except Exception as e:
                    print(f"Warning: Could not delete message {message['MessageId']}: {e}")
    except Exception as e:
        print(f"Warning: Error clearing queue: {e}")


def _get_fifo_messages(queue_name: str, config: dict) -> Dict[str, Any]:
    """Helper function to get NEW messages from SQS FIFO queue.
    Clears the queue first, then waits for new messages (max 5 seconds).
    
    Args:
        queue_name: Name of the FIFO queue (without .fifo suffix)
        config: Configuration dictionary containing accountId
        
    Returns:
        Dictionary containing status and messages
    """
    try:
        region = "ap-northeast-2"
        account_id = config['accountId']
    except KeyError as e:
        return {"error": f"Missing required configuration key: {e}"}
    
    # Create SQS client
    try:
        sqs = boto3.client('sqs', region_name=region)
    except Exception as e:
        return {"error": f"Failed to create SQS client: {e}"}
    
    # Construct SQS FIFO queue URL
    queue_url = f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}.fifo"
    
    # Check queue access first
    try:
        sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=['All'])
    except Exception as e:
        return {"error": f"Cannot access SQS queue: {e}. Please check queue name, AWS credentials, and permissions."}
    
    # Step 1: Clear all old messages from the queue
    print(f"Clearing old messages from {queue_name} queue...")
    _clear_queue(queue_name, config, sqs)
    
    # Step 2: Wait for new messages (max 5 seconds, check every 1 second)
    print(f"Waiting for new messages from {queue_name} queue...")
    max_attempts = 5
    current_time = datetime.now()
    
    for attempt in range(max_attempts):
        # Wait 1 second before checking
        time.sleep(1)
        
        # Try to receive new messages
        try:
            response = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=3,
                WaitTimeSeconds=0,
                MessageAttributeNames=['All']
            )
        except Exception as e:
            return {"error": f"Error receiving messages: {e}"}
        
        messages = response.get('Messages', [])
        
        if messages:
            # Found new messages! Process them
            print(f"Found {len(messages)} new message(s) on attempt {attempt + 1}")
            processed_messages = []
            
            for message in messages:
                try:
                    # Parse message body
                    message_body = json.loads(message['Body'])
                    
                    # Add message_id to the original message format
                    message_body["message_id"] = message['MessageId']
                    processed_messages.append(message_body)
                    
                    # Delete the message after processing
                    try:
                        sqs.delete_message(
                            QueueUrl=queue_url,
                            ReceiptHandle=message['ReceiptHandle']
                        )
                    except Exception as e:
                        print(f"Warning: Could not delete message {message['MessageId']}: {e}")
                        
                except json.JSONDecodeError:
                    # Handle non-JSON messages
                    raw_message = {
                        "message_id": message['MessageId'],
                        "raw_body": message['Body']
                    }
                    processed_messages.append(raw_message)
                    
                    # Delete the message
                    try:
                        sqs.delete_message(
                            QueueUrl=queue_url,
                            ReceiptHandle=message['ReceiptHandle']
                        )
                    except Exception as e:
                        print(f"Warning: Could not delete message {message['MessageId']}: {e}")
            
            return {
                "status": "success",
                "message_count": len(processed_messages),
                "timestamp": current_time.isoformat(),
                "messages": processed_messages
            }
    
    # No messages received within 5 seconds
    print(f"No new messages received from {queue_name} queue after {max_attempts} seconds")
    return {
        "status": "no_messages",
        "message": f"No messages available in the {queue_name} queue",
        "timestamp": current_time.isoformat()
    }


@tool
def get_robot_feedback():
    """Get the latest robot feedback information.
    This tool retrieves feedback about robot actions and command execution results.

    Args:
        None

    Returns:
        A list of robot feedback messages with timestamps and execution details.
    """
    try:
        # Load configuration
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'config.json')
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except FileNotFoundError:
            return {"error": f"config.json not found at {config_path}"}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON in config.json: {e}"}
        
        # Use helper function to get messages
        result = _get_fifo_messages("robo_feedback", config)
        
        if "error" in result:
            return result
        
        return result
        
    except Exception as e:
        return {
            "error": f"Unexpected error in get_robot_feedback: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }


@tool
def get_robot_detection():
    """Get the latest robot detection information.
    This tool retrieves emergency situation detection data including emergency_situation, explosion, fire, person_down 
    and the S3 path of the detected image file.

    Args:
        None

    Returns:
        A list of robot detection messages with timestamps, detection details, and S3 image paths.
        Detection types include: emergency_situation, explosion, fire, person_down
    """
    try:
        # Load configuration
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'config.json')
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except FileNotFoundError:
            return {"error": f"config.json not found at {config_path}"}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON in config.json: {e}"}
        
        # Use helper function to get messages
        result = _get_fifo_messages("robo_detection", config)
        
        if "error" in result:
            return result
        
        # Add presigned image URLs to messages
        result = _add_presigned_urls_to_messages(result)
        
        return result
        
    except Exception as e:
        return {
            "error": f"Unexpected error in get_robot_detection: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }


@tool
def get_robot_gesture():
    """Get the latest robot gesture information.
    This tool retrieves human gesture recognition data including what gesture the detected person is making
    and the S3 path of the gesture image file.

    Args:
        None

    Returns:
        A list of robot gesture messages with timestamps, gesture details, and S3 image paths.
        Contains information about recognized human gestures and corresponding image files.
    """
    try:
        # Load configuration
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'config.json')
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except FileNotFoundError:
            return {"error": f"config.json not found at {config_path}"}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON in config.json: {e}"}
        
        # Use helper function to get messages
        result = _get_fifo_messages("robo_gesture", config)
        
        if "error" in result:
            return result
        
        # Add presigned image URLs to messages
        result = _add_presigned_urls_to_messages(result)
        
        return result
        
    except Exception as e:
        return {
            "error": f"Unexpected error in get_robot_gesture: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }


@tool
def analyze_robot_image(image_path: str) -> str:
    """Analyze a specific robot image from S3 using Bedrock Converse API.
    
    Args:
        image_path: S3 path to the image to analyze
        
    Returns:
        Analysis result of the image
    """
    try:        
        # Download image from S3
        image_bytes = download_image_from_s3(image_path)
                
        # Initialize Bedrock client
        bedrock = boto3.client('bedrock-runtime', region_name='us-west-2')
        
        # Prepare the message for Bedrock Converse API
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "text": "보이는 이미지에 대한 내용을 설명하세요. 감지된 객체, 환경의 물리적 상태, 시각적으로 확인되는 요소들을 객관적으로 분석해주세요."
                    },
                    {
                        "image": {
                            "format": "png",
                            "source": {
                                "bytes": image_bytes
                            }
                        }
                    }
                ]
            }
        ]
        
        # Call Bedrock Converse API
        response = bedrock.converse(
            modelId="us.amazon.nova-lite-v1:0",
            messages=messages,
        )
        
        # Extract the response text
        if 'output' in response and 'message' in response['output']:
            content = response['output']['message']['content']
            if isinstance(content, list) and len(content) > 0:
                return content[0]['text']
            elif isinstance(content, str):
                return content
        
        return "이미지 분석 결과를 가져올 수 없습니다."
        
    except Exception as e:
        return f"Error analyzing image {image_path}: {str(e)}"



def extract_image_path_from_data(data_json: str, data_type: str = "detection") -> str:
    """Extract S3 image path from detection or gesture data JSON string.
    
    Args:
        data_json: JSON string containing detection or gesture data
        data_type: Type of data ("detection" or "gesture")
        
    Returns:
        S3 image path if found, error message otherwise
    """
    try:
        data = json.loads(data_json)
        
        # Determine the message key based on data type
        message_key = f"robot_{data_type}_messages"
        
        if message_key in data:
            for message in data[message_key]:
                # Check message_body for filename (S3 path)
                message_body = message.get("message_body", {})
                if isinstance(message_body, dict) and "filename" in message_body:
                    return message_body["filename"]
                    
        return f"No image path found in {data_type} data"
    except Exception as e:
        return f"Error extracting image path from {data_type} data: {str(e)}"


