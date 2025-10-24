# SSO Elevator Tests

This directory contains unit tests for the SSO Elevator application.

## Running Tests

### Prerequisites

Install test dependencies (from the `src` directory):

```bash
cd src
pip install -r requirements.txt  # or use poetry install
```

### Run All Tests

```bash
cd src
pytest tests/ -v
```

### Run Specific Test Files

```bash
# Test cache module
pytest tests/test_cache.py -v

# Test configuration
pytest tests/test_config.py -v

# Test access control
pytest tests/test_access_control.py -v
```

### Run Specific Test Classes or Functions

```bash
# Run a specific test class
pytest tests/test_cache.py::TestGetCachedAccounts -v

# Run a specific test function
pytest tests/test_cache.py::TestGetCachedAccounts::test_cache_disabled_returns_none -v
```

### Run with Coverage

```bash
pytest tests/ --cov=. --cov-report=html --cov-report=term
```

This will generate an HTML coverage report in `htmlcov/index.html`.

## Test Files

- `test_cache.py` - Comprehensive tests for the cache module covering:
  - Cache configuration
  - Cache hit/miss/expired scenarios
  - Error handling (table doesn't exist, wrong name, missing permissions)
  - Fallback behavior
  - All fail-safe mechanisms

- `test_config.py` - Configuration parsing and validation tests

- `test_access_control.py` - Access control decision-making tests

- `conftest.py` - Shared fixtures and test configuration

## Test Coverage

The cache tests specifically verify:

✅ **Cache disabled** - No DynamoDB calls when `cache_ttl_minutes = 0`  
✅ **Table doesn't exist** - Graceful fallback to API  
✅ **Wrong table name** - Graceful fallback to API  
✅ **Missing IAM permissions** - Graceful fallback to API  
✅ **Cache hit** - Returns cached data without API call  
✅ **Cache miss** - Calls API and updates cache  
✅ **Cache expired** - Returns None and calls API  
✅ **Write failures** - Logged but don't crash application  
✅ **Malformed data** - Handled gracefully  
✅ **Generic exceptions** - Never escape to users  

## Writing New Tests

When adding new features, ensure tests cover:

1. **Happy path** - Normal successful operation
2. **Edge cases** - Empty data, missing fields, etc.
3. **Error cases** - AWS service errors, network issues
4. **Fail-safe behavior** - Application continues despite errors

Example test structure:

```python
class TestYourFeature:
    """Tests for your feature."""

    def test_happy_path(self):
        """Test normal successful operation."""
        # Arrange
        # Act
        # Assert

    def test_error_handling(self):
        """Test that errors are handled gracefully."""
        # Should not raise exception
```

## Continuous Integration

These tests should be run in CI/CD pipelines before deployment to ensure:

- No regressions
- New features work correctly
- Error handling remains robust

