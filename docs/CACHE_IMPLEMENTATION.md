# AWS Accounts and Permission Sets Caching Implementation

## Overview

This implementation adds caching for AWS accounts and permission sets using DynamoDB to improve resilience during AWS service outages. If the AWS Organizations API or SSO Admin API becomes unavailable, SSO Elevator will automatically fall back to cached data.

## Implementation Details

### Infrastructure Changes

1. **DynamoDB Table** (`dynamodb.tf`):
   - Table name: Configurable via `cache_table_name` variable (default: `sso-elevator-cache`)
   - Hash key: `cache_key` (type: String) - identifies the type of cached data (accounts or permission_sets)
   - Range key: `item_id` (type: String) - identifies specific items (e.g., SSO instance ARN for permission sets)
   - TTL enabled on `ttl` attribute for automatic expiration
   - Billing mode: PAY_PER_REQUEST for cost efficiency
   - **Compliance Features**:
     - Point-in-time recovery enabled for backup compliance
     - Deletion protection enabled to prevent accidental deletion
     - Server-side encryption with AWS managed key by default (aws/dynamodb)
     - Optional customer-managed KMS key support
   - **Conditional Creation**: Table is only created when `cache_ttl_minutes > 0`

2. **Variables** (`vars.tf`):
   - `cache_table_name`: Name of the DynamoDB table (default: `sso-elevator-cache`)
   - `cache_ttl_minutes`: TTL in minutes for cached data (default: 360 = 6 hours)
     - Set to `0` to disable caching completely (no DynamoDB table will be created)
   - `cache_kms_key_arn`: Optional ARN of a customer-managed KMS key for encryption (default: null)
     - When null, uses AWS managed encryption key (aws/dynamodb)
     - When provided, uses the specified customer-managed KMS key
   - Variables are passed to Lambda functions as environment variables

3. **IAM Permissions**:
   - DynamoDB permissions conditionally added to both Lambda functions (only when caching is enabled):
     - `dynamodb:GetItem`
     - `dynamodb:PutItem`
     - `dynamodb:Query`
     - `dynamodb:Scan`

4. **Outputs** (`outputs.tf`):
   - `cache_dynamodb_table_name`: The name of the cache DynamoDB table (null if caching is disabled)
   - `cache_dynamodb_table_arn`: The ARN of the cache DynamoDB table (null if caching is disabled)

### Application Changes

1. **New Cache Module** (`src/cache.py`):
   - `CacheConfig`: Configuration class for cache settings
   - `get_cached_accounts()`: Retrieve cached accounts from DynamoDB
   - `set_cached_accounts()`: Store accounts in DynamoDB cache
   - `get_cached_permission_sets()`: Retrieve cached permission sets
   - `set_cached_permission_sets()`: Store permission sets in cache
   - `with_cache_fallback()`: Generic function for cache-first, API-fallback pattern

2. **Updated Organizations Module** (`src/organizations.py`):
   - `list_accounts_with_cache()`: Lists accounts with cache fallback
   - `get_accounts_from_config_with_cache()`: Gets filtered accounts with cache fallback
   - Fallback behavior:
     1. Try to get accounts from cache
     2. If cache miss or expired, call Organizations API
     3. Update cache with fresh data from API
     4. Return accounts to caller

3. **Updated SSO Module** (`src/sso.py`):
   - `list_permission_sets_with_cache()`: Lists permission sets with cache fallback
   - `get_permission_sets_from_config_with_cache()`: Gets filtered permission sets with cache
   - `get_account_assignment_information_with_cache()`: Combined function for both cached data
   - Same fallback behavior as organizations module

4. **Updated Config** (`src/config.py`):
   - Added `cache_table_name` field (default: `sso-elevator-cache`)
   - Added `cache_ttl_minutes` field (default: 360 = 6 hours)

5. **Updated Lambda Handlers**:
   - `src/main.py`: Updated to use cached functions and pass DynamoDB client
   - `src/revoker.py`: Updated to use cached functions for account assignments

## Cache Behavior

### Cache Key Structure

The cache uses a two-level key structure:

1. **Accounts Cache**:
   - `cache_key`: `"accounts"`
   - `item_id`: `"all"`
   - Stores all accounts in a single item

2. **Permission Sets Cache**:
   - `cache_key`: `"permission_sets"`
   - `item_id`: SSO instance ARN
   - Stores all permission sets for a specific SSO instance

### Cache Lifecycle

1. **First Request**: Cache miss → API call → Cache update → Return data
2. **Subsequent Requests**: Cache hit (if within TTL) → Return cached data
3. **After TTL Expiry**: Cache expired → API call → Cache update → Return data
4. **API Unavailable**: Use cached data even if expired (resilience feature)
5. **Cache Unavailable**: Fall back to direct API calls

### Error Handling

- All cache operations are wrapped in try-except blocks
- Cache failures never prevent the application from functioning
- Warnings are logged when cache operations fail
- DynamoDB unavailability triggers fallback to Organizations API

## Configuration

### Default Configuration

Caching is **enabled by default** with a 6-hour TTL:

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  # These are the defaults (no need to specify):
  # cache_ttl_minutes = 360  # 6 hours
  # cache_table_name  = "sso-elevator-cache"
  # cache_kms_key_arn = null  # Uses AWS managed key
}
```

### Disabling Cache

To disable caching entirely (no DynamoDB table will be created), set `cache_ttl_minutes = 0`:

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  cache_ttl_minutes = 0  # Disable caching completely
}
```

### Adjusting TTL

To adjust cache duration (e.g., 2 hours):

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  cache_ttl_minutes = 120  # 2 hours
}
```

### Custom Table Name

To use a custom DynamoDB table name:

```hcl
module "aws_sso_elevator" {
  source = "path/to/module"
  
  # Other configuration...
  
  cache_table_name = "my-custom-sso-elevator-cache"
}
```

### Custom KMS Key for Encryption

To use a customer-managed KMS key instead of the AWS managed key:

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

- `"Cache hit for accounts"`: Accounts retrieved from cache
- `"Cache miss for accounts"`: No cached accounts found
- `"Cache expired for accounts"`: Cached data past TTL
- `"Cache hit for permission sets"`: Permission sets retrieved from cache
- `"Cache miss for permission sets"`: No cached permission sets found
- `"Failed to get cached accounts"`: Error reading from cache
- `"Failed to cache accounts"`: Error writing to cache
- `"Fetching accounts from API"`: Falling back to Organizations API
- `"Using cached accounts"`: Successfully using cached data

### DynamoDB Metrics

Monitor these CloudWatch metrics for the cache table:

- `ConsumedReadCapacityUnits`: Cache read activity
- `ConsumedWriteCapacityUnits`: Cache write activity
- `UserErrors`: Failed operations (permissions issues)

## Security Considerations

1. **Data Sensitivity**: Cache contains account IDs, names, and permission set information
2. **Encryption**: 
   - Server-side encryption is **enabled by default** using AWS managed key (aws/dynamodb)
   - Optional customer-managed KMS key support via `cache_kms_key_arn` variable
   - Encryption at rest is always active
3. **Access Control**: IAM permissions limit cache access to Lambda functions only
4. **Backup & Recovery**: Point-in-time recovery is enabled for backup compliance
5. **Deletion Protection**: Table has deletion protection enabled to prevent accidental deletion
6. **TTL**: 6-hour default TTL reduces stale data risk while improving resilience

## Performance Impact

- **Cache Hit**: ~10-50ms (DynamoDB read latency)
- **Cache Miss + API Call**: Same as before + cache write overhead (~5-10ms)
- **Cost**: DynamoDB charges apply (~$0.25 per million read/write requests)

## Backward Compatibility

- Existing deployments will automatically get caching enabled with default settings (6-hour TTL)
- Original non-cached functions remain available in the codebase
- No breaking changes to module interface
- Cache can be completely disabled by setting `cache_ttl_minutes = 0` (no DynamoDB table will be created)
- If you upgrade from a previous version with caching, note the TTL default has changed from 60 minutes to 360 minutes

## Testing Recommendations

1. **Test Cache Hit**: Submit access request → Wait < TTL → Submit another request
2. **Test Cache Miss**: Submit access request → Wait > TTL → Submit another request
3. **Test API Fallback**: Disable DynamoDB table → Verify requests still work
4. **Test Cache Disabled**: Set `cache_ttl_minutes = 0` → Verify normal operation

## Troubleshooting

### "Failed to get cached accounts" warnings

- Check Lambda IAM permissions include DynamoDB read access
- Verify DynamoDB table exists and is accessible
- Check table name matches `cache_table_name` variable

### "Failed to cache accounts" warnings

- Check Lambda IAM permissions include DynamoDB write access
- Verify table has capacity (shouldn't be an issue with PAY_PER_REQUEST)
- Check CloudWatch Logs for detailed error messages

### Stale data in cache

- Reduce `cache_ttl_minutes` value
- Manually clear cache by deleting items from DynamoDB table
- Disable and re-enable caching to force refresh

## Migration from Non-Cached Version

No special migration steps required. Simply:

1. Apply Terraform changes
2. Module will create DynamoDB table
3. Cache will populate on next access request
4. Monitor CloudWatch Logs for cache activity
