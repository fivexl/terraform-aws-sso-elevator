# AWS Accounts and Permission Sets Caching Implementation

## Overview

This implementation adds caching for AWS accounts and permission sets using S3 to improve resilience during AWS service outages. The cache uses a parallel API/cache call strategy with automatic cache updates when data changes.

## Caching Strategy

### Parallel API and Cache Calls

The implementation uses a resilient caching strategy:

1. **Parallel Execution**: Both AWS API and S3 cache are called simultaneously using ThreadPoolExecutor
2. **API Success Path**: 
   - If API call succeeds, compare the result with cached data
   - If data differs, update the cache automatically
   - Return the fresh API data
3. **API Failure Path**:
   - If API call fails but cache has data, return cached data as fallback
   - Log warning about using cached data
4. **Both Fail Path**:
   - If both API and cache fail, raise an exception

### Benefits

- **No TTL Management**: Cache is kept indefinitely and updated automatically when data changes
- **Maximum Resilience**: Always tries API first, falls back to cache on failure
- **Automatic Updates**: Cache stays fresh without manual intervention
- **Parallel Performance**: API and cache calls don't block each other

## Implementation Details

### Infrastructure Changes

1. **S3 Bucket** (`s3.tf`):
   - Uses the same `fivexl/account-baseline/aws//modules/s3_baseline` module as the audit bucket
   - Bucket name: Configurable via `cache_bucket_name` variable (default: `sso-elevator-cache`)
   - Cache structure:
     - `accounts.json` - stores all accounts
     - `permission_sets/<arn_hash>.json` - stores permission sets per SSO instance
   - No TTL metadata - cache is kept indefinitely
   - **Security Features**:
     - Server-side encryption enabled (AES256 by default, KMS optional via `cache_kms_key_arn`)
     - Public access blocked (via s3_baseline module)
     - Versioning enabled
     - Lifecycle policy to clean up old versions after 7 days
   - **Conditional Creation**: Bucket is only created when `cache_enabled = true`

2. **Variables** (`vars.tf`):
   - `cache_bucket_name`: Name of the S3 bucket (default: `sso-elevator-cache`)
   - `cache_enabled`: Enable/disable caching (default: `true`)
   - `cache_kms_key_arn`: Optional ARN of a customer-managed KMS key for encryption (default: null)
   - Variables are passed to Lambda functions as environment variables

3. **IAM Permissions**:
   - S3 permissions conditionally added to both Lambda functions (only when caching is enabled):
     - `s3:GetObject`
     - `s3:PutObject`
     - `s3:ListBucket`

4. **Outputs** (`outputs.tf`):
   - `cache_s3_bucket_name`: The name of the cache S3 bucket (null if caching is disabled)
   - `cache_s3_bucket_arn`: The ARN of the cache S3 bucket (null if caching is disabled)

### Application Changes

1. **Updated Cache Module** (`src/cache.py`):
   - `CacheConfig`: Configuration class for cache settings (no TTL field)
   - `get_cached_accounts()`: Retrieve cached accounts from S3
   - `set_cached_accounts()`: Store accounts in S3 cache
   - `get_cached_permission_sets()`: Retrieve cached permission sets
   - `set_cached_permission_sets()`: Store permission sets in cache
   - `with_cache_resilience()`: New function implementing parallel API/cache strategy with automatic updates

2. **Updated Organizations Module** (`src/organizations.py`):
   - `list_accounts_with_cache()`: Lists accounts with cache resilience
   - `get_accounts_from_config_with_cache()`: Gets filtered accounts with cache resilience
   - Uses parallel API/cache calls with automatic cache updates

3. **Updated SSO Module** (`src/sso.py`):
   - `list_permission_sets_with_cache()`: Lists permission sets with cache resilience
   - `get_permission_sets_from_config_with_cache()`: Gets filtered permission sets with cache resilience
   - `get_account_assignment_information_with_cache()`: Combined function for both cached data
   - Uses parallel API/cache calls with automatic cache updates

4. **Updated Config** (`src/config.py`):
   - `cache_bucket_name`: S3 bucket name (default: `sso-elevator-cache`)
   - `cache_enabled`: Boolean flag to enable/disable caching (default: `True`)
   - Removed `cache_ttl_minutes` field

5. **Updated Lambda Handlers**:
   - `src/main.py`: Updated to use S3 client instead of DynamoDB client
   - `src/revoker.py`: Updated to use S3 client for account assignments

## Cache Behavior

### Cache Key Structure

The cache uses S3 object keys:

1. **Accounts Cache**:
   - Key: `accounts.json`
   - Stores all accounts in a single JSON file

2. **Permission Sets Cache**:
   - Key: `permission_sets/<arn_hash>.json`
   - Stores all permission sets for a specific SSO instance
   - ARN is hashed (colons and slashes replaced with underscores) for safe file naming

### Cache Lifecycle

1. **First Request**: 
   - API and cache called in parallel
   - Cache miss (no data)
   - API succeeds → Store in cache → Return API data

2. **Subsequent Requests (Data Unchanged)**:
   - API and cache called in parallel
   - Both succeed
   - Data matches → No cache update → Return API data

3. **Subsequent Requests (Data Changed)**:
   - API and cache called in parallel
   - Both succeed
   - Data differs → Update cache → Return API data

4. **API Unavailable**:
   - API and cache called in parallel
   - API fails, cache succeeds
   - Return cached data (logged as fallback)

5. **Cache Unavailable**:
   - API and cache called in parallel
   - Cache fails, API succeeds
   - Store in cache → Return API data

### Error Handling

**The cache is designed to be fail-safe:**

- All cache operations are wrapped in try-except blocks
- **Cache failures NEVER prevent the application from functioning**
- Warnings are logged when cache operations fail
- S3 unavailability automatically triggers fallback to direct AWS API calls
- Application works normally if:
  - S3 bucket doesn't exist
  - Bucket name is misconfigured
  - IAM permissions are missing
  - S3 service is down
  
**Behavior on errors:**
- **Cache read errors**: Log warning, continue with API data only
- **Cache write errors**: Log warning, return API data without caching
- **API errors with cache available**: Log warning, return cached data
- **Both fail**: Raise exception (application cannot function)

## Configuration

### Default Configuration

Caching is **enabled by default**:

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  # These are the defaults (no need to specify):
  # cache_enabled     = true
  # cache_bucket_name = "sso-elevator-cache"
  # cache_kms_key_arn = null  # Uses AES256 encryption
}
```

### Disabling Cache

To disable caching entirely (no S3 bucket will be created):

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  cache_enabled = false  # Disable caching completely
}
```

### Custom Bucket Name

To use a custom S3 bucket name:

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  cache_bucket_name = "my-custom-sso-elevator-cache"
}
```

### Custom KMS Key for Encryption

To use a customer-managed KMS key instead of AES256:

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  cache_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
}
```

## Monitoring

### CloudWatch Logs

Cache operations are logged with the following patterns:

- `"Retrieved X accounts from cache"`: Accounts retrieved from cache
- `"Cache miss for accounts"`: No cached accounts found
- `"Successfully fetched accounts from API"`: API call succeeded
- `"API call failed for accounts"`: API call failed
- `"API data differs from cache"`: Cache will be updated
- `"API data matches cache"`: No cache update needed
- `"API failed for accounts, using cached data as fallback"`: Using cache due to API failure
- `"Failed to get cached accounts"`: Error reading from cache
- `"Failed to cache accounts"`: Error writing to cache

### S3 Metrics

Monitor these CloudWatch metrics for the cache bucket:

- `NumberOfObjects`: Number of cached items
- `BucketSizeBytes`: Total cache size
- `AllRequests`: Cache read/write activity

## Security Considerations

1. **Data Sensitivity**: Cache contains account IDs, names, and permission set information
2. **Encryption**: 
   - Server-side encryption is **enabled by default** using AES256
   - Optional customer-managed KMS key support via `cache_kms_key_arn` variable
   - Encryption at rest is always active
3. **Access Control**: IAM permissions limit cache access to Lambda functions only
4. **Public Access**: All public access is blocked by default
5. **Versioning**: Enabled to protect against accidental overwrites
6. **No Expiration**: Cache is kept indefinitely and updated automatically when data changes

## Performance Impact

- **Parallel Calls**: API and cache calls execute simultaneously (~50-100ms for cache)
- **Cache Hit + API Success**: Same latency as API-only (cache runs in parallel)
- **API Failure with Cache**: ~50-100ms (S3 GetObject latency)
- **Cost**: S3 charges apply (~$0.005 per 1,000 GET requests, ~$0.005 per 1,000 PUT requests)

## Backward Compatibility

- Existing deployments will need to update their Terraform configuration
- The variable `cache_ttl_minutes` has been removed and replaced with `cache_enabled`
- Cache behavior has changed from TTL-based to automatic update-based
- No data migration is needed as the cache will refresh automatically

## Migration from TTL-Based Cache

If you're migrating from the TTL-based cache:

1. Update your Terraform configuration to remove `cache_ttl_minutes` and use `cache_enabled` instead
2. Apply the Terraform changes
3. The cache will continue to work with the new strategy
4. No manual intervention needed

## Testing Recommendations

### Integration Testing

1. **Test Normal Operation**: Submit access request → Verify both API and cache are called
2. **Test Cache Update**: Change AWS data → Submit request → Verify cache is updated
3. **Test API Fallback**: Temporarily block API access → Verify cached data is used
4. **Test Cache Disabled**: Set `cache_enabled = false` → Verify normal operation (no S3 calls)
5. **Test Missing Permissions**: Temporarily remove S3 IAM permissions → Verify graceful fallback to API

## Troubleshooting

### "Failed to get cached accounts" warnings

**Important:** These warnings do not break functionality. The application will continue to work using API data.

Common causes:
- S3 bucket doesn't exist (check if `cache_enabled` is set correctly in Terraform)
- Wrong bucket name (verify `cache_bucket_name` matches between Terraform and Lambda environment variables)
- Lambda IAM permissions missing S3 read access

**How to diagnose:**
1. Check CloudWatch Logs for the full error message
2. Verify the bucket exists: `aws s3 ls s3://sso-elevator-cache`
3. Confirm bucket name environment variable: Check Lambda configuration `CACHE_BUCKET_NAME`
4. Verify IAM permissions include `s3:GetObject`, `s3:ListBucket`

### "Failed to cache accounts" warnings

**Important:** These warnings do not break functionality. The application will continue to work with API data.

Common causes:
- S3 bucket doesn't exist
- Wrong bucket name configuration
- Lambda IAM permissions missing S3 write access

**How to diagnose:**
1. Check CloudWatch Logs for detailed error messages
2. Verify IAM permissions include `s3:PutObject`

### "API failed, using cached data as fallback"

This is expected behavior when AWS APIs are unavailable. The application is working correctly by using cached data.

### Cache not updating

If you notice the cache isn't updating when AWS data changes:
1. Check CloudWatch Logs for cache write errors
2. Verify S3 permissions include `s3:PutObject`
3. Check if data comparison is working correctly (logs will show "API data differs from cache")
