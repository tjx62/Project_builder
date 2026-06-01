import os
import boto3
from botocore.exceptions import ClientError

# ----------------------------------------------------------------
# Set your Bedrock API key here, or set it as an environment variable:
#
#   Mac/Linux:  export AWS_BEARER_TOKEN_BEDROCK="your-key-here"
#   Windows:    setx AWS_BEARER_TOKEN_BEDROCK "your-key-here"
#
# If you already set it as an environment variable, remove or comment
# out the line below.
# ----------------------------------------------------------------
os.environ["AWS_BEARER_TOKEN_BEDROCK"] = ""

# Create a Bedrock Runtime client
client = boto3.client("bedrock-runtime", region_name="us-east-1")

# Claude Sonnet 4.6 model ID
model_id = "us.anthropic.claude-sonnet-4-6"

# Build the request using the Converse API
try:
    response = client.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": "Say hello and tell me which model you are, in one sentence."
                    }
                ],
            }
        ],
        inferenceConfig={"maxTokens": 256, "temperature": 0.5},
    )

    # Extract and print the response
    reply = response["output"]["message"]["content"][0]["text"]
    print("✅ Success! Claude responded:")
    print(reply)

    # Print token usage
    usage = response["usage"]
    print(f"\nTokens — Input: {usage['inputTokens']}, Output: {usage['outputTokens']}")

except ClientError as e:
    error_code = e.response["Error"]["Code"]
    error_msg = e.response["Error"]["Message"]
    print(f"❌ AWS error ({error_code}): {error_msg}")
    if error_code == "UnauthorizedException":
        print("   → Double-check your API key is correct and hasn't expired.")
    elif error_code == "AccessDeniedException":
        if "INVALID_PAYMENT_INSTRUMENT" in error_msg:
            print("   → Payment issue detected. Check the following:")
            print("      1. Go to AWS Console → Billing → Payment methods and verify your card is valid and not expired.")
            print("      2. Go to Bedrock → Model catalog and confirm model access is fully approved.")
            print("      3. If you just fixed a billing issue, wait ~30 minutes and try again.")
        else:
            print("   → Your key may not have access to this model. Check model access in the Bedrock console.")
except Exception as e:
    print(f"❌ Unexpected error: {e}")
