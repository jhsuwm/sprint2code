# Contributing to Sprint2Code

Thank you for your interest in contributing to Sprint2Code! This guide will help you understand our development standards and best practices.

## Logging Standards

One of the core requirements for maintaining code quality in Sprint2Code is **consistent logging practices**. All logging throughout the codebase follows a standardized approach.

### Logging Best Practices

#### 1. **Use Wrapper Functions (Required)**

We use **wrapper functions** for all logging throughout the codebase. This ensures consistent caller information (module:function) and better debugging capabilities.

**✅ CORRECT:**
```python
from log_config import info, debug, error, warning, critical

# In your module/function
info("User login successful")
error("Failed to process vacation plan")
debug("Query params: {params}")
warning("Deprecated API endpoint used")
```

**❌ INCORRECT - DO NOT USE:**
```python
import logging

logger = logging.getLogger(__name__)
logger.info("User login successful")  # ❌ Don't use direct logger calls
```

#### 2. **Import Logging Functions Correctly**

All Python files should import logging functions from `log_config`:

```python
# At the top of your file
from log_config import info, debug, error, warning, critical
```

**Never use** `import logging` to create your own logger instance.

#### 3. **Available Logging Functions**

Use the appropriate function for your message level:

- `info()` - General informational messages (default for most logs)
- `debug()` - Detailed debugging information
- `warning()` - Warning messages for potentially problematic situations
- `error()` - Error messages for failure conditions
- `critical()` - Critical errors that may cause application failure

#### 4. **Automatic Caller Detection**

Our logging system automatically captures:
- **Module name** - The file/module where the log originated
- **Function name** - The function/method that called the logging function
- **User ID and Session ID** - Automatically captured from context

**You don't need to manually pass this information.** It's captured automatically:

```
[2026-04-02T10:48:27.414385] [INFO] [user_123:session_456] [agents.auto_fix_worker:process_job] Auto-fix job completed
                                                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ Captured automatically!
```

#### 5. **User Context (Optional but Recommended)**

For tracking logs by user across a request, set user context at the start of a request handler:

```python
from log_config import set_user_context, clear_user_context

@app.post("/api/endpoint")
async def handle_request(request):
    # Set user context
    set_user_context(user_id="user_123", session_id="session_456")
    
    try:
        # Logging calls here will automatically include user context
        info("Processing user request")
        # ... do work ...
    finally:
        # Clear context when done
        clear_user_context()
```

#### 6. **Log Message Format**

Keep log messages:
- **Clear and concise** - State what happened, not how it happened
- **Actionable** - Include relevant details for debugging
- **Consistent** - Use similar format across similar operations

**✅ Good examples:**
```python
info(f"User {email} logged in successfully")
error(f"Failed to save vacation plan '{plan_name}': {str(error)}")
debug(f"Query returned {count} results in {duration_ms}ms")
```

**❌ Poor examples:**
```python
info("done")  # Too vague
error(exception)  # Should be f"Error: {exception}"
debug("executing method X")  # Use actual function name
```

#### 7. **Sensitive Data**

**NEVER log:**
- Passwords or authentication tokens
- API keys or secrets
- Personal financial information  
- Full email addresses (except in user action logs)
- Full phone numbers

**✅ SAFE:**
```python
info(f"OAuth token received for {email_domain}")  # email domain only
error(f"Failed to authenticate with provider")  # No token details
```

**❌ UNSAFE:**
```python
info(f"OAuth token: {oauth_token}")  # ❌ Logging sensitive token
debug(f"API key: {api_key}")  # ❌ Never log API keys
```

#### 8. **API Metrics Logging**

For tracking API calls (Google AI, Cloud services, etc.), use the metrics logging system:

```python
from log_config import google_ai_metrics, google_api_metrics

# For Google AI/Gemini calls
await google_ai_metrics.log_google_ai_call(
    api_name='gemini-2.5-flash',
    model_name='gemini-2.5-flash',
    input_tokens=150,
    output_tokens=300,
    response_time_ms=1250,
    success=True
)

# For other Google APIs (Maps, Firestore, Storage, etc.)
await google_api_metrics.log_google_api_call(
    api_name='google_maps',
    function_name='places_search',
    response_time_ms=450
)
```

## Code Review Checklist for Logging

When submitting a Pull Request, ensure:

- [ ] All `import logging` statements removed (except in infrastructure files)
- [ ] All `logger = logging.getLogger(__name__)` removed (except in infrastructure files)
- [ ] All logging uses wrapper functions: `info()`, `error()`, etc.
- [ ] User context is set for request handlers using `set_user_context()`
- [ ] No sensitive data (tokens, API keys, passwords) in log messages
- [ ] Log messages are clear and helpful for debugging
- [ ] Error logging includes the exception details: `error(f"Operation failed: {e}")`

## Files NOT Requiring Wrapper Functions

The following infrastructure/internal logging files manage the logging system itself and may use Python's standard logging:

- `backend/log_config.py` - Logging configuration and initialization
- `backend/utils/enhanced_logging.py` - Enhanced logging implementation

All other files **must** use the wrapper function style.

## Testing Your Logging

To verify your logging works correctly:

```python
from log_config import info, error

# Test in your function
info("This is a test message")

# You should see output like:
# [2026-04-02T10:49:20.349773] [INFO] [unknown:unknown] [your_module:your_function] This is a test message
#                                                         ^^^^^^^^^^^^^^^^^^^^^^^^ Automatically captured!
```

## Questions?

If you have questions about the logging system or these standards:
1. Check the inline documentation in `backend/log_config.py`
2. Review the test examples in the codebase
3. Ask in the project's discussion/issues section

---

Thank you for following these logging standards! Consistent logging helps all contributors debug issues faster and maintain code quality across the project.
