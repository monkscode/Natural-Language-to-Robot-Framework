"""
Authentication and authorization utilities for the API.

This module provides authentication and rate limiting functionality
for healing operations and other sensitive API endpoints.
"""

import time
import logging
from typing import Dict, Optional
from fastapi import HTTPException, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

# Simple in-memory rate limiting storage
# In production, this would use Redis or similar
rate_limit_storage: Dict[str, Dict[str, float]] = {}

# Security scheme for bearer token authentication
security = HTTPBearer(auto_error=False)

# Mock user database - in production, this would be a real user store
MOCK_USERS = {
    "admin": {
        "username": "admin",
        "role": "admin",
        "permissions": ["healing:read", "healing:write", "healing:admin"]
    },
    "user": {
        "username": "user", 
        "role": "user",
        "permissions": ["healing:read"]
    }
}

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Dict[str, str]:
    """
    Get the current authenticated user.
    
    For development purposes, this uses a simple token-based authentication.
    In production, this would integrate with a proper authentication system.
    """
    # For development, allow requests without authentication
    # In production, this should be removed
    if not credentials:
        logger.warning("Request without authentication credentials - allowing for development")
        return {
            "username": "anonymous",
            "role": "user",
            "permissions": ["healing:read"]
        }
    
    token = credentials.credentials
    
    # Simple token validation - in production, use proper JWT validation
    if token == "admin-token":
        return MOCK_USERS["admin"]
    elif token == "user-token":
        return MOCK_USERS["user"]
    else:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"}
        )

def check_permission(user: Dict[str, str], required_permission: str) -> bool:
    """Check if user has the required permission."""
    user_permissions = user.get("permissions", [])
    return required_permission in user_permissions

async def require_permission(permission: str):
    """Dependency to require a specific permission."""
    def permission_checker(user: Dict[str, str] = Depends(get_current_user)):
        if not check_permission(user, permission):
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions. Required: {permission}"
            )
        return user
    return permission_checker

async def rate_limit_healing(request: Request) -> None:
    """
    Rate limiting for healing operations.
    
    Limits healing operations to prevent abuse and resource exhaustion.
    """
    client_ip = request.client.host
    current_time = time.time()
    
    # Rate limit: 10 requests per minute per IP
    rate_limit_window = 60  # seconds
    max_requests = 10
    
    if client_ip not in rate_limit_storage:
        rate_limit_storage[client_ip] = {}
    
    client_data = rate_limit_storage[client_ip]
    
    # Clean up old entries
    cutoff_time = current_time - rate_limit_window
    client_data = {
        timestamp: count for timestamp, count in client_data.items()
        if timestamp > cutoff_time
    }
    rate_limit_storage[client_ip] = client_data
    
    # Count requests in current window
    total_requests = sum(client_data.values())
    
    if total_requests >= max_requests:
        logger.warning(f"Rate limit exceeded for IP {client_ip}: {total_requests} requests in {rate_limit_window}s")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Too many healing requests.",
            headers={"Retry-After": str(rate_limit_window)}
        )
    
    # Record this request
    minute_bucket = int(current_time // 60) * 60
    client_data[minute_bucket] = client_data.get(minute_bucket, 0) + 1
    
    logger.debug(f"Rate limit check passed for IP {client_ip}: {total_requests + 1}/{max_requests} requests")

class RateLimiter:
    """Generic rate limiter class for different endpoints."""
    
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.storage: Dict[str, Dict[float, int]] = {}
    
    async def check_rate_limit(self, request: Request, identifier: Optional[str] = None) -> None:
        """Check rate limit for a request."""
        if identifier is None:
            identifier = request.client.host
        
        current_time = time.time()
        
        if identifier not in self.storage:
            self.storage[identifier] = {}
        
        client_data = self.storage[identifier]
        
        # Clean up old entries
        cutoff_time = current_time - self.window_seconds
        client_data = {
            timestamp: count for timestamp, count in client_data.items()
            if timestamp > cutoff_time
        }
        self.storage[identifier] = client_data
        
        # Count requests in current window
        total_requests = sum(client_data.values())
        
        if total_requests >= self.max_requests:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {total_requests}/{self.max_requests} requests in {self.window_seconds}s",
                headers={"Retry-After": str(self.window_seconds)}
            )
        
        # Record this request
        bucket = int(current_time // 60) * 60
        client_data[bucket] = client_data.get(bucket, 0) + 1

# Pre-configured rate limiters for different endpoints
healing_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
config_rate_limiter = RateLimiter(max_requests=5, window_seconds=300)  # 5 requests per 5 minutes

async def rate_limit_config(request: Request) -> None:
    """Rate limiting for configuration changes."""
    await config_rate_limiter.check_rate_limit(request)