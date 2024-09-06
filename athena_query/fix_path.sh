#!/bin/bash
# In releases previous to 2.0.0, Elevator didn't check for double slashes in the path, which could cause Athena to fail.
# If your S3 bucket has double slashes in the path, you can run this script to fix the path.
# Replace SOURCE_PREFIX, DESTINATION_PREFIX, and BUCKET_NAME with your values.

set -x  # Enable debugging

SOURCE_PREFIX="logs//2024/"
DESTINATION_PREFIX="logs/2024/"
BUCKET_NAME=""

# Set the number of parallel jobs (adjust based on your system's resources)
NUM_JOBS=10

# Function to move a single file
move_file() {
  FILE_PATH="$1"
  BUCKET_NAME="$2"
  NEW_FILE_PATH=$(echo "$FILE_PATH" | sed "s|//|/|g")

  # Copy the file to the new path with encryption
  aws s3 cp "s3://$BUCKET_NAME/$FILE_PATH" "s3://$BUCKET_NAME/$NEW_FILE_PATH" --sse AES256
  
  # Delete the original file if the copy was successful
  if [[ $? -eq 0 ]]; then
    aws s3 rm "s3://$BUCKET_NAME/$FILE_PATH"
    echo "Moved $FILE_PATH to $NEW_FILE_PATH"
  else
    echo "Error copying $FILE_PATH"
  fi
}

export -f move_file  # Export the function to be used by parallel

# Step 1: List all objects in the first path and move them asynchronously
aws s3 ls "s3://$BUCKET_NAME/$SOURCE_PREFIX" --recursive | awk '{print $4}' | \
  grep -v '^$' | xargs -P "$NUM_JOBS" -I {} bash -c 'move_file "$@"' _ {} "$BUCKET_NAME"
