"""
Production-oriented single-file Flask backend for a pizza / restaurant ordering platform.

Pip dependencies actually used:
    Flask
    firebase-admin
    google-cloud-firestore
    python-dotenv
    Flask-WTF
    Flask-Limiter
    requests

This file intentionally keeps all backend code in one module for the first implementation.
Large media files are not stored in Firestore; only URLs/metadata are stored.
"""

from __future__ import annotations

BUILD_TAG = "realistic_demo_easypaisa_request_flow_20260824"

import hashlib
import importlib
import base64
import json
import logging
import math
import os
import re
from pathlib import Path
import secrets
import smtplib
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
from functools import wraps
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import firebase_admin
import requests
from dotenv import load_dotenv
from firebase_admin import auth, credentials, firestore
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from google.api_core.exceptions import FailedPrecondition
from google.cloud.firestore_v1 import aggregation
from google.cloud.firestore_v1.base_query import FieldFilter
from werkzeug.middleware.proxy_fix import ProxyFix


# =============================================================================
# Environment / configuration
# =============================================================================

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV in {"production", "prod"}

if IS_PRODUCTION and not os.getenv("FLASK_SECRET_KEY"):
    raise RuntimeError("FLASK_SECRET_KEY must be configured in production.")

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(48)
BUSINESS_TIMEZONE_NAME = os.getenv("BUSINESS_TIMEZONE", "Asia/Karachi")
try:
    BUSINESS_TZ = ZoneInfo(BUSINESS_TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(f"Invalid BUSINESS_TIMEZONE: {BUSINESS_TIMEZONE_NAME}") from exc

BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "").strip()
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS", os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")).strip()
FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY", "").strip()
FIREBASE_WEB_AUTH_DOMAIN = os.getenv(
    "FIREBASE_WEB_AUTH_DOMAIN",
    f"{FIREBASE_PROJECT_ID}.firebaseapp.com" if FIREBASE_PROJECT_ID else "",
).strip()
FIREBASE_WEB_APP_ID = os.getenv("FIREBASE_WEB_APP_ID", "").strip()
FIREBASE_WEB_MESSAGING_SENDER_ID = os.getenv("FIREBASE_WEB_MESSAGING_SENDER_ID", "").strip()
FIREBASE_WEB_STORAGE_BUCKET = os.getenv("FIREBASE_WEB_STORAGE_BUCKET", "").strip()
FIREBASE_GOOGLE_AUTH_ENABLED = os.getenv("FIREBASE_GOOGLE_AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
FIREBASE_FACEBOOK_AUTH_ENABLED = os.getenv("FIREBASE_FACEBOOK_AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

SESSION_MINUTES = int(os.getenv("SESSION_MINUTES", "480"))
RECENT_AUTH_SECONDS = int(os.getenv("RECENT_AUTH_SECONDS", "300"))

# Small clock differences between a browser/device and Google's token issuer should
# not make a freshly-issued Firebase ID token fail as "used too early".
# Firebase Admin officially allows 0..60 seconds of verification clock skew.
FIREBASE_CLOCK_SKEW_SECONDS = max(
    0,
    min(int(os.getenv("FIREBASE_CLOCK_SKEW_SECONDS", "5")), 60),
)

# Multi-image product uploads need a larger request envelope than ordinary JSON forms.
# Every individual image and the number of files are still constrained separately below.
MAX_CONTENT_LENGTH_MB = int(os.getenv("MAX_CONTENT_LENGTH_MB", "80"))
DEFAULT_PAGE_SIZE = max(1, min(int(os.getenv("DEFAULT_PAGE_SIZE", "25")), 100))
MAX_PAGE_SIZE = max(DEFAULT_PAGE_SIZE, min(int(os.getenv("MAX_PAGE_SIZE", "100")), 200))

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME).strip()
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Restaurant").strip()

EASYPAISA_ENABLED = os.getenv("EASYPAISA_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
EASYPAISA_ENDPOINT = os.getenv("EASYPAISA_ENDPOINT", "").strip()
EASYPAISA_MERCHANT_ID = os.getenv("EASYPAISA_MERCHANT_ID", "").strip()
EASYPAISA_ADAPTER = os.getenv("EASYPAISA_ADAPTER", "").strip()
EASYPAISA_TIMEOUT_SECONDS = int(os.getenv("EASYPAISA_TIMEOUT_SECONDS", "20"))

ALLOW_ZERO_DELIVERY_FEE = os.getenv("ALLOW_ZERO_DELIVERY_FEE", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_DELIVERY_FEE_PKR = os.getenv("DEFAULT_DELIVERY_FEE_PKR", "").strip()

INITIAL_OWNER_UID = os.getenv("INITIAL_OWNER_UID", "").strip()
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Restaurant").strip()
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "").strip()

# Demo payment gateway.
# Outside production this simulates the shape of an Easypaisa wallet request:
# the restaurant asks the provider to charge the customer's wallet, the customer
# approves/declines on a provider-style page, and the backend records the result.
# No real Easypaisa network call or real-money movement occurs in demo mode.
DEMO_EASYPAISA_ENABLED = os.getenv(
    "DEMO_EASYPAISA_ENABLED",
    "false" if IS_PRODUCTION else "true",
).lower() in {"1", "true", "yes", "on"}

RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://").strip()
CSP_POLICY = os.getenv(
    "CONTENT_SECURITY_POLICY",
    "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
).strip()

# Development-friendly Firestore index fallback.
# Production should normally deploy explicit composite indexes instead of scanning.
ALLOW_FIRESTORE_INDEX_FALLBACK = os.getenv(
    "ALLOW_FIRESTORE_INDEX_FALLBACK",
    "false" if IS_PRODUCTION else "true",
).lower() in {"1", "true", "yes", "on"}
FIRESTORE_FALLBACK_SCAN_LIMIT = max(100, min(int(os.getenv("FIRESTORE_FALLBACK_SCAN_LIMIT", "1000")), 5000))

# Product gallery stores URL metadata only. The main/cover image remains in
# product.image_url; gallery_images contains additional ordered image URLs.
MAX_PRODUCT_GALLERY_IMAGES = max(
    1,
    min(int(os.getenv("MAX_PRODUCT_GALLERY_IMAGES", "10")), 20),
)

# Local product image uploads. Browsers never expose C:/D:/G: paths to Flask;
# they stream the user-selected files in a multipart request. Files are stored
# beneath Flask's static/images/products/<product-id>/ directory.
MAX_PRODUCT_IMAGE_UPLOAD_MB = max(
    1,
    min(int(os.getenv("MAX_PRODUCT_IMAGE_UPLOAD_MB", "6")), 12),
)
MAX_PRODUCT_IMAGE_BYTES = MAX_PRODUCT_IMAGE_UPLOAD_MB * 1024 * 1024
MAX_PRODUCT_UPLOAD_FILES = MAX_PRODUCT_GALLERY_IMAGES + 1  # cover + ordered extras
PRODUCT_IMAGE_UPLOAD_FIELD = "product_images"
PRODUCT_GALLERY_UPLOAD_FIELD = "gallery_files"
PRODUCT_IMAGE_STATIC_SUBDIR = Path("images") / "products"


# =============================================================================
# Flask setup
# =============================================================================

app = Flask(__name__)
app.config.update(
    SECRET_KEY=FLASK_SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=SESSION_MINUTES),
    MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH_MB * 1024 * 1024,
    WTF_CSRF_TIME_LIMIT=7200,
    WTF_CSRF_SSL_STRICT=IS_PRODUCTION,
)

if os.getenv("TRUST_PROXY", "true").lower() in {"1", "true", "yes", "on"}:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

csrf = CSRFProtect(app)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[os.getenv("GLOBAL_RATE_LIMIT", "300 per minute")],
    storage_uri=RATELIMIT_STORAGE_URI,
)


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("restaurant_backend")

SENSITIVE_LOG_KEYS = {
    "password",
    "new_password",
    "id_token",
    "firebase_id_token",
    "authorization",
    "secret",
    "merchant_secret",
    "private_key",
    "smtp_password",
    "easypaisa_phone",
    "easypaisa_customer_phone",
    "payer_phone",
    "wallet_phone",
    "oobCode",
    "oob_code",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k.lower() in {x.lower() for x in SENSITIVE_LOG_KEYS} else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# =============================================================================
# Firebase setup
# =============================================================================

FIREBASE_APP: Optional[firebase_admin.App] = None
DB: Any = None
FIREBASE_INIT_ERROR: Optional[str] = None


def initialize_firebase() -> None:
    global FIREBASE_APP, DB, FIREBASE_INIT_ERROR
    try:
        try:
            FIREBASE_APP = firebase_admin.get_app()
        except ValueError:
            options: dict[str, Any] = {}
            if FIREBASE_PROJECT_ID:
                options["projectId"] = FIREBASE_PROJECT_ID
            if FIREBASE_DATABASE_URL:
                options["databaseURL"] = FIREBASE_DATABASE_URL

            credentials_json = os.getenv("FIREBASE_CREDENTIALS_JSON", "").strip()
            if credentials_json:
                if not credentials_json.startswith("{"):
                    credentials_json = base64.b64decode(credentials_json).decode("utf-8")
                cred = credentials.Certificate(json.loads(credentials_json))
            elif FIREBASE_CREDENTIALS:
                if not os.path.exists(FIREBASE_CREDENTIALS):
                    raise RuntimeError(
                        "FIREBASE_CREDENTIALS/GOOGLE_APPLICATION_CREDENTIALS points to a missing file."
                    )
                cred = credentials.Certificate(FIREBASE_CREDENTIALS)
            else:
                cred = credentials.ApplicationDefault()

            FIREBASE_APP = firebase_admin.initialize_app(cred, options=options or None)

        DB = firestore.client(app=FIREBASE_APP)
        FIREBASE_INIT_ERROR = None
        logger.info("Firebase Admin and Cloud Firestore initialized.")
    except Exception as exc:  # application intentionally remains startable for /health
        DB = None
        FIREBASE_APP = None
        FIREBASE_INIT_ERROR = f"{type(exc).__name__}: {exc}"
        logger.error("Firebase initialization failed: %s", FIREBASE_INIT_ERROR)


initialize_firebase()


# =============================================================================
# Constants and policy
# =============================================================================

ROLE_OWNER = "owner"
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_CHEF = "chef"
ROLE_CUSTOMER = "customer"

ROLES = {ROLE_OWNER, ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_CHEF, ROLE_CUSTOMER}
STAFF_ROLES = {ROLE_OWNER, ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_CHEF}
ROLE_RANK = {
    ROLE_CUSTOMER: 0,
    ROLE_CHEF: 10,
    ROLE_ADMIN: 20,
    ROLE_SUPER_ADMIN: 30,
    ROLE_OWNER: 40,
}

PERMISSIONS = {
    "view_dashboard",
    "view_orders",
    "update_orders",
    "cancel_orders",
    "view_customers",
    "manage_customers",
    "view_products",
    "manage_products",
    "view_categories",
    "manage_categories",
    "view_deals",
    "manage_deals",
    "view_reviews",
    "moderate_reviews",
    "view_finance",
    "view_reports",
    "view_locations",
    "manage_locations",
    "view_bookings",
    "manage_bookings",
    "manage_staff",
    "manage_permissions",
    "view_payments",
    "verify_payments",
    "manage_settings",
    "kitchen_access",
    "manage_hero_media",
    "view_feedback",
    "manage_feedback",
}

DEFAULT_ROLE_PERMISSIONS = {
    ROLE_OWNER: set(PERMISSIONS),
    ROLE_SUPER_ADMIN: PERMISSIONS - {"manage_settings"},
    ROLE_ADMIN: {"view_dashboard"},
    ROLE_CHEF: {"view_dashboard", "view_orders", "update_orders", "kitchen_access"},
    ROLE_CUSTOMER: set(),
}

SERVICE_MODES = {"delivery", "pickup", "dine_in"}
PAYMENT_METHODS = {"cod", "easypaisa", "bank_transfer"}
PAYMENT_STATUSES = {
    "pending",
    "awaiting_verification",
    "paid",
    "failed",
    "rejected",
    "refunded",
    "cancelled",
}

DELIVERY_STATUSES = [
    "order_placed",
    "received",
    "confirmed",
    "preparing",
    "packing",
    "rider_assigned",
    "out_for_delivery",
    "delivered",
    "cancelled",
]
PICKUP_STATUSES = [
    "order_placed",
    "confirmed",
    "preparing",
    "ready_for_pickup",
    "picked_up",
    "cancelled",
]
DINE_IN_STATUSES = [
    "order_placed",
    "confirmed",
    "sent_to_kitchen",
    "preparing",
    "ready",
    "serving",
    "served",
    "completed",
    "cancelled",
]

TERMINAL_SUCCESS_STATUS = {
    "delivery": "delivered",
    "pickup": "picked_up",
    "dine_in": "completed",
}

ORDER_TRANSITIONS = {
    "delivery": {
        "order_placed": {"received", "confirmed", "cancelled"},
        "received": {"confirmed", "cancelled"},
        "confirmed": {"preparing", "cancelled"},
        "preparing": {"packing", "cancelled"},
        "packing": {"rider_assigned", "out_for_delivery", "cancelled"},
        "rider_assigned": {"out_for_delivery", "cancelled"},
        "out_for_delivery": {"delivered", "cancelled"},
        "delivered": set(),
        "cancelled": set(),
    },
    "pickup": {
        "order_placed": {"confirmed", "cancelled"},
        "confirmed": {"preparing", "cancelled"},
        "preparing": {"ready_for_pickup", "cancelled"},
        "ready_for_pickup": {"picked_up", "cancelled"},
        "picked_up": set(),
        "cancelled": set(),
    },
    "dine_in": {
        "order_placed": {"confirmed", "sent_to_kitchen", "cancelled"},
        "confirmed": {"sent_to_kitchen", "cancelled"},
        "sent_to_kitchen": {"preparing", "cancelled"},
        "preparing": {"ready", "cancelled"},
        "ready": {"serving", "cancelled"},
        "serving": {"served", "cancelled"},
        "served": {"completed"},
        "completed": set(),
        "cancelled": set(),
    },
}

REVIEW_MODERATION_STATES = {"pending", "approved", "hidden", "rejected"}
BOOKING_STATUSES = {"pending", "confirmed", "rejected", "cancelled", "completed"}
ACCOUNT_STATUSES = {"active", "disabled"}
HERO_MEDIA_TYPES = {"image", "video"}

SAFE_ORDER_SORT_FIELDS = {
    "placed_at": "placed_at",
    "updated_at": "updated_at",
    "total": "total_minor",
    "status": "order_status",
}
SAFE_PRODUCT_SORT_FIELDS = {
    "name": "name_search",
    "price": "base_price_minor",
    "updated_at": "updated_at",
    "created_at": "created_at",
}

PLAIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\-\s()]{6,20}$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


# =============================================================================
# Application errors / responses
# =============================================================================

class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class ConfigurationError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 503)


def api_success(data: Any = None, status: int = 200, **extra: Any):
    payload: dict[str, Any] = {"success": True, "data": json_safe(data)}
    if extra:
        payload.update(json_safe(extra))
    return jsonify(payload), status


def api_error(code: str, message: str, status: int, details: Optional[dict[str, Any]] = None):
    payload: dict[str, Any] = {
        "success": False,
        "error": {"code": code, "message": message},
    }
    if details:
        payload["error"]["details"] = json_safe(details)
    return jsonify(payload), status


def wants_json_response() -> bool:
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    accepted = request.accept_mimetypes
    return accepted["application/json"] > accepted["text/html"]


# =============================================================================
# Generic utilities / validation
# =============================================================================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def business_now() -> datetime:
    return utcnow().astimezone(BUSINESS_TZ)


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return value


def require_db():
    if DB is None:
        raise ConfigurationError(
            "FIREBASE_UNAVAILABLE",
            "Firebase/Cloud Firestore is not configured or could not be initialized.",
        )
    return DB


def payload_data() -> dict[str, Any]:
    if request.is_json:
        obj = request.get_json(silent=True)
        if not isinstance(obj, dict):
            raise AppError("INVALID_JSON", "A JSON object is required.", 400)
        return obj
    return request.form.to_dict(flat=True)


def strip_control_chars(value: str) -> str:
    return CONTROL_CHARS_RE.sub("", value)


def validate_text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    min_len: int = 0,
    max_len: int = 500,
) -> str:
    text = strip_control_chars(str(value or "")).strip()
    if required and not text:
        raise AppError("VALIDATION_ERROR", f"{field} is required.", 422, {"field": field})
    if text and len(text) < min_len:
        raise AppError("VALIDATION_ERROR", f"{field} is too short.", 422, {"field": field})
    if len(text) > max_len:
        raise AppError("VALIDATION_ERROR", f"{field} is too long.", 422, {"field": field})
    return text


def validate_name(value: Any, field: str = "name", required: bool = True) -> str:
    text = validate_text(value, field, required=required, min_len=2 if required else 0, max_len=120)
    if text and not re.fullmatch(r"[\w\s.'\-]+", text, flags=re.UNICODE):
        raise AppError("VALIDATION_ERROR", f"{field} contains invalid characters.", 422, {"field": field})
    return text


def validate_email(value: Any, required: bool = True) -> str:
    email = str(value or "").strip().lower()
    if required and not email:
        raise AppError("VALIDATION_ERROR", "Email is required.", 422, {"field": "email"})
    if email and (len(email) > 254 or not EMAIL_RE.fullmatch(email)):
        raise AppError("VALIDATION_ERROR", "Invalid email address.", 422, {"field": "email"})
    return email


def validate_phone(value: Any, required: bool = True) -> str:
    phone = str(value or "").strip()
    if required and not phone:
        raise AppError("VALIDATION_ERROR", "Phone number is required.", 422, {"field": "phone"})
    if phone and not PHONE_RE.fullmatch(phone):
        raise AppError("VALIDATION_ERROR", "Invalid phone number.", 422, {"field": "phone"})
    return phone


def normalize_easypaisa_mobile(value: Any, *, required: bool = True) -> str:
    """Normalize a Pakistan mobile number for the Easypaisa wallet flow."""
    raw = strip_control_chars(str(value or "")).strip()
    if not raw:
        if required:
            raise AppError(
                "VALIDATION_ERROR",
                "Easypaisa mobile number is required.",
                422,
                {"field": "easypaisa_phone"},
            )
        return ""

    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0092"):
        digits = digits[4:]
    elif digits.startswith("92"):
        digits = digits[2:]

    if len(digits) == 10 and digits.startswith("3"):
        digits = "0" + digits

    if not re.fullmatch(r"03\d{9}", digits):
        raise AppError(
            "VALIDATION_ERROR",
            "Enter a valid Pakistan mobile number for Easypaisa.",
            422,
            {"field": "easypaisa_phone"},
        )
    return digits


def mask_mobile_number(value: Any) -> str:
    """Return a customer-safe masked mobile number for provider/demo pages."""
    phone = normalize_easypaisa_mobile(value, required=False)
    if not phone:
        return ""
    return f"{phone[:4]}***{phone[-4:]}"


def validate_new_password(value: Any) -> str:
    password = validate_text(value, "new_password", required=True, min_len=8, max_len=128)
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        raise AppError(
            "WEAK_PASSWORD",
            "Password must be at least 8 characters and include at least one letter and one number.",
            422,
        )
    return password


def validate_id(value: Any, field: str = "id") -> str:
    ident = str(value or "").strip()
    if not PLAIN_ID_RE.fullmatch(ident):
        raise AppError("VALIDATION_ERROR", f"Invalid {field}.", 422, {"field": field})
    return ident


def validate_slug(value: Any, field: str = "slug") -> str:
    slug = str(value or "").strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise AppError("VALIDATION_ERROR", f"Invalid {field}.", 422, {"field": field})
    return slug


def slugify(value: str) -> str:
    value = strip_control_chars(value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    if not value:
        raise AppError("VALIDATION_ERROR", "Unable to derive a valid slug.", 422)
    return validate_slug(value)


def validate_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise AppError("VALIDATION_ERROR", "Invalid boolean value.", 422)


def validate_int(value: Any, field: str, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AppError("VALIDATION_ERROR", f"{field} must be an integer.", 422, {"field": field})
    if number < minimum or number > maximum:
        raise AppError(
            "VALIDATION_ERROR",
            f"{field} must be between {minimum} and {maximum}.",
            422,
            {"field": field},
        )
    return number


def money_to_minor(value: Any, field: str = "amount") -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise AppError("VALIDATION_ERROR", f"Invalid {field}.", 422, {"field": field})
    if amount < 0:
        raise AppError("VALIDATION_ERROR", f"{field} cannot be negative.", 422, {"field": field})
    return int(amount * 100)


def minor_to_money(minor: int) -> str:
    return f"{(Decimal(int(minor)) / Decimal(100)):.2f}"


def money_fields(minor: int) -> dict[str, Any]:
    return {"minor": int(minor), "amount": minor_to_money(minor), "currency": "PKR"}


def validate_latitude(value: Any, required: bool = False) -> Optional[float]:
    if value is None or value == "":
        if required:
            raise AppError("VALIDATION_ERROR", "Latitude is required.", 422)
        return None
    try:
        lat = float(value)
    except (TypeError, ValueError):
        raise AppError("VALIDATION_ERROR", "Invalid latitude.", 422)
    if not -90 <= lat <= 90:
        raise AppError("VALIDATION_ERROR", "Latitude must be between -90 and 90.", 422)
    return round(lat, 7)


def validate_longitude(value: Any, required: bool = False) -> Optional[float]:
    if value is None or value == "":
        if required:
            raise AppError("VALIDATION_ERROR", "Longitude is required.", 422)
        return None
    try:
        lon = float(value)
    except (TypeError, ValueError):
        raise AppError("VALIDATION_ERROR", "Invalid longitude.", 422)
    if not -180 <= lon <= 180:
        raise AppError("VALIDATION_ERROR", "Longitude must be between -180 and 180.", 422)
    return round(lon, 7)


def validate_url(value: Any, field: str, required: bool = False) -> str:
    url = validate_text(value, field, required=required, max_len=2048)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise AppError("VALIDATION_ERROR", f"Invalid {field} URL.", 422, {"field": field})
    if IS_PRODUCTION and parsed.scheme != "https":
        raise AppError("VALIDATION_ERROR", f"{field} must use HTTPS in production.", 422, {"field": field})
    return url


def _product_image_extension(data: bytes) -> str:
    """Return a safe extension for supported raster image signatures."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise AppError(
        "UNSUPPORTED_IMAGE",
        "Only valid JPEG, PNG, or WebP product images are allowed.",
        422,
    )


def _validated_product_image_uploads(field_name: str, *, max_files: int) -> list[dict[str, Any]]:
    """Read and validate a bounded set of image uploads from the current request."""
    uploads = [item for item in request.files.getlist(field_name) if item and item.filename]
    if not uploads:
        return []
    if max_files < 1 or len(uploads) > max_files:
        raise AppError(
            "TOO_MANY_IMAGES",
            f"Select at most {max(0, max_files)} image(s) in this upload.",
            422,
            {"field": field_name},
        )

    validated: list[dict[str, Any]] = []
    for index, storage in enumerate(uploads, start=1):
        raw = storage.stream.read(MAX_PRODUCT_IMAGE_BYTES + 1)
        if not raw:
            raise AppError(
                "EMPTY_IMAGE",
                f"Selected image {index} is empty.",
                422,
                {"field": field_name},
            )
        if len(raw) > MAX_PRODUCT_IMAGE_BYTES:
            raise AppError(
                "IMAGE_TOO_LARGE",
                f"Each product image must be {MAX_PRODUCT_IMAGE_UPLOAD_MB} MB or smaller.",
                413,
                {"field": field_name},
            )
        extension = _product_image_extension(raw)
        validated.append(
            {
                "bytes": raw,
                "extension": extension,
                "original_name": validate_text(storage.filename, "image filename", max_len=255),
            }
        )
    return validated


def _save_product_image_uploads(
    product_id: str,
    uploads: list[dict[str, Any]],
    *,
    start_position: int = 1,
) -> tuple[list[str], list[Path]]:
    """Persist validated images under static/ and return browser URLs plus saved paths."""
    pid = validate_id(product_id, "product_id")
    if not uploads:
        return [], []

    static_root = Path(app.static_folder or (Path(app.root_path) / "static")).resolve()
    relative_dir = PRODUCT_IMAGE_STATIC_SUBDIR / pid
    target_dir = (static_root / relative_dir).resolve()
    try:
        target_dir.relative_to(static_root)
    except ValueError as exc:
        raise ConfigurationError("INVALID_UPLOAD_PATH", "Product image upload path is outside the static directory.") from exc

    target_dir.mkdir(parents=True, exist_ok=True)
    urls: list[str] = []
    saved_paths: list[Path] = []
    try:
        for offset, upload in enumerate(uploads):
            position = start_position + offset
            extension = str(upload["extension"])
            filename = f"image-{position:02d}-{uuid.uuid4().hex[:12]}.{extension}"
            destination = target_dir / filename
            destination.write_bytes(upload["bytes"])
            saved_paths.append(destination)
            relative_file = (relative_dir / filename).as_posix()
            urls.append(url_for("static", filename=relative_file, _external=True))
    except Exception:
        _cleanup_saved_product_images(saved_paths)
        raise
    return urls, saved_paths


def _cleanup_saved_product_images(paths: Iterable[Path]) -> None:
    """Best-effort rollback for files saved before a failed Firestore mutation."""
    parents: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        parents.add(path.parent)
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            logger.warning("Could not roll back uploaded product image %s", path)
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass


def parse_iso_datetime(value: Any, field: str, required: bool = False) -> Optional[datetime]:
    if value is None or value == "":
        if required:
            raise AppError("VALIDATION_ERROR", f"{field} is required.", 422, {"field": field})
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AppError("VALIDATION_ERROR", f"Invalid {field} datetime.", 422, {"field": field})
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BUSINESS_TZ)
    return dt.astimezone(timezone.utc)


def parse_date_value(value: Any, field: str = "date", required: bool = True) -> Optional[date]:
    if value is None or value == "":
        if required:
            raise AppError("VALIDATION_ERROR", f"{field} is required.", 422)
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise AppError("VALIDATION_ERROR", f"Invalid {field}; expected YYYY-MM-DD.", 422)


def parse_time_value(value: Any, field: str = "time", required: bool = True) -> Optional[dt_time]:
    if value is None or value == "":
        if required:
            raise AppError("VALIDATION_ERROR", f"{field} is required.", 422)
        return None
    text = str(value)
    try:
        parsed = dt_time.fromisoformat(text)
    except ValueError:
        raise AppError("VALIDATION_ERROR", f"Invalid {field}; expected HH:MM.", 422)
    return parsed.replace(second=0, microsecond=0)


def query_limit() -> int:
    raw = request.args.get("limit", DEFAULT_PAGE_SIZE)
    return validate_int(raw, "limit", 1, MAX_PAGE_SIZE)


def normalize_search(value: str) -> str:
    return re.sub(r"\s+", " ", strip_control_chars(value).strip().lower())


def safe_list_of_ids(value: Any, field: str, max_items: int = 100) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise AppError("VALIDATION_ERROR", f"{field} must be a list.", 422)
    if len(value) > max_items:
        raise AppError("VALIDATION_ERROR", f"{field} has too many items.", 422)
    return list(dict.fromkeys(validate_id(x, field) for x in value))


def get_client_ip() -> str:
    return request.remote_addr or "unknown"


def request_fingerprint() -> str:
    ua = request.headers.get("User-Agent", "")[:300]
    raw = f"{get_client_ip()}|{ua}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()


def generate_public_order_reference(order_id: str) -> str:
    """Create a human-friendly reference derived from the already-unique internal order ID."""
    now = business_now()
    safe_fragment = validate_id(order_id, "order_id")[:10].upper()
    return f"ORD-{now.strftime('%y%m%d')}-{safe_fragment}"


def generate_booking_reference() -> str:
    return f"BKG-{business_now().strftime('%y%m%d')}-{secrets.token_hex(3).upper()}"


def hash_idempotency(uid: str, key: str) -> str:
    return hashlib.sha256(f"{uid}:{key}".encode()).hexdigest()


def validate_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not 8 <= len(key) <= 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", key):
        raise AppError("INVALID_IDEMPOTENCY_KEY", "A valid Idempotency-Key is required.", 400)
    return key


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# =============================================================================
# Firestore helpers
# =============================================================================

def doc_dict(snapshot: Any) -> Optional[dict[str, Any]]:
    if snapshot is None or not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    data["id"] = snapshot.id
    return data


def get_document(collection: str, document_id: str, *, required: bool = False, code: str = "NOT_FOUND"):
    db = require_db()
    snap = db.collection(collection).document(document_id).get()
    data = doc_dict(snap)
    if required and data is None:
        raise AppError(code, "Requested resource was not found.", 404)
    return data


def firestore_query(collection: str, filters: Iterable[tuple[str, str, Any]] = ()):
    db = require_db()
    q: Any = db.collection(collection)
    for field, op, value in filters:
        q = q.where(filter=FieldFilter(field, op, value))
    return q


def _is_missing_firestore_index_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return isinstance(exc, FailedPrecondition) and ("requires an index" in text or "query requires an index" in text)


def _python_sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (5, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float, Decimal)):
        return (1, float(value))
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return (2, dt.timestamp())
    if isinstance(value, date):
        return (3, value.isoformat())
    return (4, str(value).casefold())


def _nested_value(data: dict[str, Any], field: str) -> Any:
    current: Any = data
    for part in str(field).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _matches_filter(data: dict[str, Any], field: str, op: str, expected: Any) -> bool:
    actual = _nested_value(data, field)
    try:
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op == "<":
            return actual is not None and actual < expected
        if op == "<=":
            return actual is not None and actual <= expected
        if op == ">":
            return actual is not None and actual > expected
        if op == ">=":
            return actual is not None and actual >= expected
        if op == "in":
            return actual in set(expected or [])
        if op == "not_in":
            return actual not in set(expected or [])
        if op == "array_contains":
            return isinstance(actual, list) and expected in actual
        if op == "array_contains_any":
            expected_values = set(expected or [])
            return isinstance(actual, list) and any(item in expected_values for item in actual)
    except (TypeError, ValueError):
        return False
    return False


def _scan_collection_with_filters(
    collection: str,
    filters: Iterable[tuple[str, str, Any]] = (),
    *,
    scan_limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Development fallback for a missing composite index.

    This is intentionally bounded and exists so a fresh/demo Firestore database can
    render safely while indexes are still being created. Production defaults to
    disabling this fallback.
    """
    db = require_db()
    max_docs = min(scan_limit or FIRESTORE_FALLBACK_SCAN_LIMIT, FIRESTORE_FALLBACK_SCAN_LIMIT)
    docs = list(db.collection(collection).limit(max_docs).stream())
    items = [doc_dict(d) for d in docs]
    filter_list = list(filters)
    return [
        item
        for item in items
        if item and all(_matches_filter(item, field, op, value) for field, op, value in filter_list)
    ]


def ordered_documents(
    collection: str,
    filters: Iterable[tuple[str, str, Any]] = (),
    *,
    order_field: str,
    direction: str = "asc",
    limit: int = 100,
) -> list[dict[str, Any]]:
    filter_list = list(filters)
    direction_value = firestore.Query.DESCENDING if direction == "desc" else firestore.Query.ASCENDING
    try:
        docs = list(
            firestore_query(collection, filter_list)
            .order_by(order_field, direction=direction_value)
            .limit(limit)
            .stream()
        )
        return [x for x in (doc_dict(d) for d in docs) if x]
    except FailedPrecondition as exc:
        if not (_is_missing_firestore_index_error(exc) and ALLOW_FIRESTORE_INDEX_FALLBACK):
            raise
        logger.warning(
            "Missing Firestore composite index for %s ordered by %s; using bounded development fallback.",
            collection,
            order_field,
        )
        items = _scan_collection_with_filters(collection, filter_list)
        items.sort(key=lambda item: _python_sort_key(_nested_value(item, order_field)), reverse=direction == "desc")
        return items[:limit]


def filtered_documents(
    collection: str,
    filters: Iterable[tuple[str, str, Any]] = (),
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    filter_list = list(filters)
    try:
        docs = list(firestore_query(collection, filter_list).limit(limit).stream())
        return [x for x in (doc_dict(d) for d in docs) if x]
    except FailedPrecondition as exc:
        if not (_is_missing_firestore_index_error(exc) and ALLOW_FIRESTORE_INDEX_FALLBACK):
            raise
        logger.warning("Missing Firestore composite index for %s; using bounded development fallback.", collection)
        return _scan_collection_with_filters(collection, filter_list)[:limit]


def first_document(collection: str, filters: Iterable[tuple[str, str, Any]] = ()) -> Optional[dict[str, Any]]:
    items = filtered_documents(collection, filters, limit=1)
    return items[0] if items else None


def _normalize_firestore_filter_operator(raw: Any) -> Optional[str]:
    """Normalize Firestore filter operators for the bounded development fallback."""
    if raw is None:
        return None
    text = str(getattr(raw, "name", raw)).strip()
    direct = {
        "==": "==",
        "!=": "!=",
        "<": "<",
        "<=": "<=",
        ">": ">",
        ">=": ">=",
        "in": "in",
        "not_in": "not_in",
        "not-in": "not_in",
        "array_contains": "array_contains",
        "array-contains": "array_contains",
        "array_contains_any": "array_contains_any",
        "array-contains-any": "array_contains_any",
    }
    lowered = text.lower()
    if lowered in direct:
        return direct[lowered]
    enum_map = {
        "EQUAL": "==",
        "NOT_EQUAL": "!=",
        "LESS_THAN": "<",
        "LESS_THAN_OR_EQUAL": "<=",
        "GREATER_THAN": ">",
        "GREATER_THAN_OR_EQUAL": ">=",
        "IN": "in",
        "NOT_IN": "not_in",
        "ARRAY_CONTAINS": "array_contains",
        "ARRAY_CONTAINS_ANY": "array_contains_any",
    }
    return enum_map.get(text.upper())


def _query_fallback_filters(query: Any) -> Optional[list[tuple[str, str, Any]]]:
    """
    Best-effort extraction of simple Firestore field filters.

    This is used only for the bounded development fallback. Production still
    expects the proper Firestore indexes to exist.
    """
    raw_filters = getattr(query, "_field_filters", None)
    if raw_filters is None:
        return None

    extracted: list[tuple[str, str, Any]] = []

    def walk(item: Any) -> bool:
        nested = getattr(item, "filters", None)
        if nested is not None:
            for child in nested:
                if not walk(child):
                    return False
            return True

        field = getattr(item, "field_path", None)
        if not field:
            field_obj = getattr(item, "field", None)
            field = getattr(field_obj, "field_path", None) if field_obj is not None else None

        op = getattr(item, "op_string", None)
        if op is None:
            op = getattr(item, "op", None)
        op_n = _normalize_firestore_filter_operator(op)

        # google-cloud-firestore's public FieldFilter exposes the Python value
        # through `.value`. If a future client version exposes only a protobuf
        # Value, fail closed rather than returning an incorrect result.
        value = getattr(item, "value", None)

        if not field or not op_n:
            return False
        extracted.append((str(field), op_n, value))
        return True

    for item in raw_filters:
        if not walk(item):
            return None
    return extracted


def _query_fallback_collection(query: Any) -> Optional[str]:
    parent = getattr(query, "_parent", None)
    collection_id = getattr(parent, "id", None)
    return str(collection_id) if collection_id else None


def count_query(
    query: Any,
    *,
    fallback_collection: Optional[str] = None,
    fallback_filters: Iterable[tuple[str, str, Any]] = (),
) -> int:
    try:
        aggregate_query = aggregation.AggregationQuery(query)
        aggregate_query.count(alias="count")
        results = aggregate_query.get()
        if not results:
            return 0
        return int(results[0][0].value)
    except FailedPrecondition as exc:
        if not (_is_missing_firestore_index_error(exc) and ALLOW_FIRESTORE_INDEX_FALLBACK):
            raise

        collection = fallback_collection or _query_fallback_collection(query)
        filters = list(fallback_filters)
        if not filters:
            extracted = _query_fallback_filters(query)
            if extracted is not None:
                filters = extracted

        if not collection:
            raise

        logger.warning(
            "Missing Firestore index for aggregate count on %s; using bounded development fallback.",
            collection,
        )
        return len(_scan_collection_with_filters(collection, filters))


def paginate_query(
    collection: str,
    query: Any,
    *,
    order_field: str,
    direction: str = "desc",
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    db = require_db()
    page_limit = limit or query_limit()
    direction_value = firestore.Query.DESCENDING if direction == "desc" else firestore.Query.ASCENDING
    base_query = query
    ordered_query = base_query.order_by(order_field, direction=direction_value)
    cursor_id = validate_id(cursor, "cursor") if cursor else None
    if cursor_id:
        cursor_snap = db.collection(collection).document(cursor_id).get()
        if not cursor_snap.exists:
            raise AppError("INVALID_CURSOR", "Pagination cursor is invalid.", 400)
        ordered_query = ordered_query.start_after(cursor_snap)
    try:
        docs = list(ordered_query.limit(page_limit + 1).stream())
        has_more = len(docs) > page_limit
        docs = docs[:page_limit]
        items = [doc_dict(d) for d in docs]
        next_cursor = docs[-1].id if has_more and docs else None
        return [x for x in items if x], next_cursor
    except FailedPrecondition as exc:
        if not (_is_missing_firestore_index_error(exc) and ALLOW_FIRESTORE_INDEX_FALLBACK):
            raise
        logger.warning(
            "Missing Firestore composite index for paginated %s ordered by %s; using bounded development fallback.",
            collection,
            order_field,
        )
        fallback_filters = _query_fallback_filters(base_query)
        if fallback_filters is None:
            # Do not silently return incorrect data if the client library
            # changes its internal filter representation.
            raise
        items = _scan_collection_with_filters(collection, fallback_filters)
        items.sort(key=lambda item: _python_sort_key(_nested_value(item, order_field)), reverse=direction == "desc")
        start = 0
        if cursor_id:
            cursor_positions = [idx for idx, item in enumerate(items) if item.get("id") == cursor_id]
            if not cursor_positions:
                raise AppError("INVALID_CURSOR", "Pagination cursor is outside the fallback scan window.", 400)
            start = cursor_positions[0] + 1
        page = items[start:start + page_limit + 1]
        has_more = len(page) > page_limit
        page = page[:page_limit]
        next_cursor = page[-1]["id"] if has_more and page else None
        return page, next_cursor


def set_server_timestamps(data: dict[str, Any], *, creating: bool = False) -> dict[str, Any]:
    out = dict(data)
    if creating:
        out.setdefault("created_at", firestore.SERVER_TIMESTAMP)
    out["updated_at"] = firestore.SERVER_TIMESTAMP
    return out


def audit_log(
    action: str,
    target_type: str,
    target_id: str,
    metadata: Optional[dict[str, Any]] = None,
    *,
    actor: Optional[dict[str, Any]] = None,
) -> None:
    try:
        db = require_db()
        actor = actor or getattr(g, "principal", None) or {}
        safe_metadata = redact(metadata or {})
        db.collection("audit_logs").add(
            {
                "actor_uid": actor.get("uid"),
                "actor_role": actor.get("role"),
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "metadata": safe_metadata,
                "ip_hash": hashlib.sha256(get_client_ip().encode()).hexdigest(),
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
    except Exception:
        logger.exception("Audit log write failed for action=%s target=%s:%s", action, target_type, target_id)


# =============================================================================
# Email delivery / Firebase account action links
# =============================================================================

def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM_EMAIL)


def send_email_message(to_email: str, subject: str, text_body: str) -> bool:
    if not smtp_configured():
        logger.warning("Email delivery is not configured; message not sent.")
        return False
    message = EmailMessage()
    message["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>" if SMTP_FROM_NAME else SMTP_FROM_EMAIL
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)
        return True
    except Exception:
        logger.exception("Email delivery failed.")
        return False


def firebase_action_settings() -> Optional[auth.ActionCodeSettings]:
    continue_url = os.getenv("FIREBASE_ACTION_CONTINUE_URL", f"{BASE_URL}/login")
    if not continue_url:
        return None
    return auth.ActionCodeSettings(url=continue_url, handle_code_in_app=False)


def send_password_reset_email_server_side(email: str) -> bool:
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        link = auth.generate_password_reset_link(email, action_code_settings=firebase_action_settings(), app=FIREBASE_APP)
    except auth.EmailNotFoundError:
        return True  # neutral behavior: user-facing caller never distinguishes
    except Exception:
        logger.exception("Could not generate Firebase password reset link.")
        return False
    if not smtp_configured():
        logger.warning("Password reset link generated but SMTP is not configured; link is not exposed.")
        return False
    return send_email_message(
        email,
        f"Reset your {BUSINESS_NAME} password",
        "We received a request to reset your password.\n\n"
        f"Use this secure Firebase password reset link:\n{link}\n\n"
        "If you did not request this, you can ignore this email.",
    )


def send_verification_email_server_side(email: str) -> bool:
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        link = auth.generate_email_verification_link(email, action_code_settings=firebase_action_settings(), app=FIREBASE_APP)
    except Exception:
        logger.exception("Could not generate Firebase email verification link.")
        return False
    if not smtp_configured():
        logger.warning("Verification link generated but SMTP is not configured; link is not exposed.")
        return False
    return send_email_message(
        email,
        f"Verify your {BUSINESS_NAME} email",
        f"Please verify your email address using this secure Firebase link:\n{link}",
    )


def identity_toolkit_reset_password(payload: dict[str, Any]) -> dict[str, Any]:
    if not FIREBASE_WEB_API_KEY:
        raise ConfigurationError(
            "FIREBASE_WEB_API_KEY_MISSING",
            "FIREBASE_WEB_API_KEY is required for the custom reset-password action handler.",
        )
    endpoint = f"https://identitytoolkit.googleapis.com/v1/accounts:resetPassword?key={FIREBASE_WEB_API_KEY}"
    try:
        response = requests.post(endpoint, json=payload, timeout=15)
    except requests.RequestException as exc:
        logger.exception("Firebase Identity Toolkit resetPassword request failed.")
        raise AppError("AUTH_SERVICE_UNAVAILABLE", "Authentication service is temporarily unavailable.", 503) from exc
    data = response.json() if response.content else {}
    if response.ok:
        return data
    error_message = str((data.get("error") or {}).get("message", "PASSWORD_RESET_FAILED"))
    mapping = {
        "EXPIRED_OOB_CODE": ("RESET_CODE_EXPIRED", "This password reset link has expired.", 400),
        "INVALID_OOB_CODE": ("RESET_CODE_INVALID", "This password reset link is invalid or has already been used.", 400),
        "USER_DISABLED": ("ACCOUNT_DISABLED", "This account is disabled.", 403),
        "WEAK_PASSWORD": ("WEAK_PASSWORD", "The new password does not meet Firebase password requirements.", 422),
    }
    code, message, status = mapping.get(error_message.split(" : ")[0], ("PASSWORD_RESET_FAILED", "Password reset could not be completed.", 400))
    raise AppError(code, message, status)


# =============================================================================
# Authentication and authorization helpers
# =============================================================================

@dataclass
class Principal:
    uid: str
    role: str
    email: str
    name: str
    status: str
    permissions: set[str]
    email_verified: bool
    provider: str
    auth_time: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "role": self.role,
            "email": self.email,
            "name": self.name,
            "status": self.status,
            "permissions": sorted(self.permissions),
            "email_verified": self.email_verified,
            "provider": self.provider,
            "auth_time": self.auth_time,
        }


def allowed_provider(decoded: dict[str, Any]) -> str:
    firebase_claim = decoded.get("firebase") or {}
    provider = str(firebase_claim.get("sign_in_provider") or "").strip()
    allowed = {"password", "google.com", "facebook.com", "custom"}
    if provider not in allowed:
        raise AppError("AUTH_PROVIDER_NOT_ALLOWED", "This authentication provider is not allowed.", 401)
    return provider


def verify_firebase_id_token(id_token: str, *, check_revoked: bool = True) -> tuple[dict[str, Any], Any]:
    if not id_token:
        raise AppError("AUTH_TOKEN_REQUIRED", "Firebase ID token is required.", 401)
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        decoded = auth.verify_id_token(
            id_token,
            check_revoked=check_revoked,
            app=FIREBASE_APP,
            clock_skew_seconds=FIREBASE_CLOCK_SKEW_SECONDS,
        )
        uid = decoded.get("uid") or decoded.get("sub")
        if not uid:
            raise AppError("INVALID_AUTH_TOKEN", "Token does not contain a valid UID.", 401)
        user_record = auth.get_user(uid, app=FIREBASE_APP)
        if user_record.disabled:
            raise AppError("ACCOUNT_DISABLED", "This account is disabled.", 403)
        token_email = (decoded.get("email") or "").strip().lower()
        auth_email = (user_record.email or "").strip().lower()
        if token_email and auth_email and token_email != auth_email:
            raise AppError("AUTH_IDENTITY_MISMATCH", "Authenticated identity does not match the account.", 401)
        allowed_provider(decoded)
        return decoded, user_record
    except AppError as exc:
        logger.warning("Authentication request rejected: %s", exc.code)
        raise
    except auth.RevokedIdTokenError as exc:
        logger.warning("Firebase ID token rejected: revoked token.")
        raise AppError("AUTH_TOKEN_REVOKED", "Your session has been revoked. Please sign in again.", 401) from exc
    except auth.ExpiredIdTokenError as exc:
        logger.warning("Firebase ID token rejected: expired token.")
        raise AppError("AUTH_TOKEN_EXPIRED", "Your sign-in token has expired. Please sign in again.", 401) from exc
    except auth.InvalidIdTokenError as exc:
        logger.warning("Firebase ID token rejected as invalid: %s", exc)
        raise AppError("INVALID_AUTH_TOKEN", "Invalid authentication token.", 401) from exc
    except auth.UserDisabledError as exc:
        logger.warning("Firebase Authentication rejected a disabled user.")
        raise AppError("ACCOUNT_DISABLED", "This account is disabled.", 403) from exc
    except Exception as exc:
        logger.exception("Firebase token verification failed.")
        raise AppError("AUTH_VERIFICATION_FAILED", "Authentication could not be verified.", 401) from exc


def ensure_customer_profile(decoded: dict[str, Any], user_record: Any) -> dict[str, Any]:
    db = require_db()
    uid = user_record.uid
    ref = db.collection("users").document(uid)
    snap = ref.get()
    now_fields = {"updated_at": firestore.SERVER_TIMESTAMP, "last_login_at": firestore.SERVER_TIMESTAMP}
    verified = bool(decoded.get("email_verified", user_record.email_verified))
    provider = allowed_provider(decoded)

    if not snap.exists:
        profile = {
            "uid": uid,
            "full_name": user_record.display_name or "",
            "email": (user_record.email or decoded.get("email") or "").lower(),
            "phone": user_record.phone_number or "",
            "city": "",
            "area": "",
            "address": "",
            "latitude": None,
            "longitude": None,
            "marketing_notifications_enabled": False,
            "role": ROLE_CUSTOMER,
            "status": "active",
            "email_verified": verified,
            "auth_provider": provider,
            "created_at": firestore.SERVER_TIMESTAMP,
            **now_fields,
        }
        ref.set(profile)
        return {**profile, "id": uid, "created_at": utcnow(), "updated_at": utcnow(), "last_login_at": utcnow()}

    profile = snap.to_dict() or {}
    existing_role = profile.get("role", ROLE_CUSTOMER)
    # Existing trusted staff identities stay staff; browser input never controls this field.
    updates = {
        **now_fields,
        "email": (user_record.email or decoded.get("email") or profile.get("email") or "").lower(),
        "email_verified": verified,
        "auth_provider": provider,
    }
    ref.set(updates, merge=True)
    profile.update(updates)
    profile["role"] = existing_role
    profile["id"] = uid
    return profile


def resolve_principal(uid: str) -> Principal:
    db = require_db()
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        auth_user = auth.get_user(uid, app=FIREBASE_APP)
    except auth.UserNotFoundError as exc:
        raise AppError("AUTH_ACCOUNT_NOT_FOUND", "Authenticated account no longer exists.", 401) from exc
    except Exception as exc:
        logger.exception("Could not validate Firebase account state for an existing Flask session.")
        raise AppError("AUTH_SERVICE_UNAVAILABLE", "Authentication service is temporarily unavailable.", 503) from exc
    if auth_user.disabled:
        raise AppError("ACCOUNT_DISABLED", "This account is disabled.", 403)

    user_snap = db.collection("users").document(uid).get()
    if not user_snap.exists:
        raise AppError("PROFILE_NOT_FOUND", "Authenticated user profile was not found.", 401)
    user = user_snap.to_dict() or {}
    status = user.get("status", "active")
    if status != "active":
        raise AppError("ACCOUNT_DISABLED", "This account is disabled.", 403)
    role = user.get("role", ROLE_CUSTOMER)
    if role not in ROLES:
        raise AppError("INVALID_ACCOUNT_ROLE", "Account role is invalid.", 403)

    permissions: set[str] = set()
    if role in STAFF_ROLES:
        staff_snap = db.collection("staff").document(uid).get()
        if not staff_snap.exists:
            raise AppError("STAFF_PROFILE_MISSING", "Staff profile is missing.", 403)
        staff = staff_snap.to_dict() or {}
        if staff.get("status", "active") != "active":
            raise AppError("ACCOUNT_DISABLED", "This staff account is disabled.", 403)
        trusted_role = staff.get("role")
        if trusted_role not in STAFF_ROLES or trusted_role != role:
            raise AppError("STAFF_ROLE_MISMATCH", "Staff role configuration is inconsistent.", 403)
        raw_permissions = staff.get("permissions", [])
        permissions = {p for p in raw_permissions if p in PERMISSIONS}
        if role == ROLE_OWNER:
            permissions = set(PERMISSIONS)
    auth_time = int(session.get("auth_time") or 0)
    return Principal(
        uid=uid,
        role=role,
        email=str(user.get("email") or ""),
        name=str(user.get("full_name") or ""),
        status=status,
        permissions=permissions,
        email_verified=bool(user.get("email_verified", False)),
        provider=str(session.get("provider") or user.get("auth_provider") or ""),
        auth_time=auth_time,
    )


@app.before_request
def load_principal_from_session() -> None:
    g.principal = None
    uid = session.get("uid")
    if not uid:
        return
    try:
        principal = resolve_principal(uid)
        g.principal = principal.as_dict()
    except AppError:
        session.clear()
        if request.endpoint not in {"logout", "health"}:
            logger.info("Protected session invalidated for uid=%s", uid)


def current_principal() -> dict[str, Any]:
    principal = getattr(g, "principal", None)
    if not principal:
        raise AppError("AUTH_REQUIRED", "Authentication is required.", 401)
    return principal


def login_required(view: Callable):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        current_principal()
        return view(*args, **kwargs)
    return wrapped


def customer_required(view: Callable):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        p = current_principal()
        if p["role"] != ROLE_CUSTOMER:
            raise AppError("CUSTOMER_ONLY", "This action is available to customer accounts only.", 403)
        return view(*args, **kwargs)
    return wrapped


def staff_required(view: Callable):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        p = current_principal()
        if p["role"] not in STAFF_ROLES:
            raise AppError("STAFF_ONLY", "Staff access is required.", 403)
        return view(*args, **kwargs)
    return wrapped


def owner_required(view: Callable):
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        p = current_principal()
        if p["role"] != ROLE_OWNER:
            raise AppError("OWNER_ONLY", "Owner access is required.", 403)
        return view(*args, **kwargs)
    return wrapped


def permission_required(permission: str):
    if permission not in PERMISSIONS:
        raise RuntimeError(f"Unknown permission in decorator: {permission}")

    def decorator(view: Callable):
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any):
            p = current_principal()
            if p["role"] not in STAFF_ROLES:
                raise AppError("ACCESS_DENIED", "Staff access is required.", 403)
            if p["role"] != ROLE_OWNER and permission not in set(p.get("permissions", [])):
                raise AppError(
                    "PERMISSION_DENIED",
                    "You do not have permission to access this section. Please contact your Super Admin.",
                    403,
                    {"required_permission": permission},
                )
            return view(*args, **kwargs)
        return wrapped
    return decorator


def require_recent_firebase_token(id_token: str, uid: str) -> dict[str, Any]:
    decoded, _ = verify_firebase_id_token(id_token, check_revoked=True)
    token_uid = decoded.get("uid") or decoded.get("sub")
    if token_uid != uid:
        raise AppError("AUTH_IDENTITY_MISMATCH", "Recent authentication identity mismatch.", 401)
    auth_time = int(decoded.get("auth_time") or 0)
    now_epoch = int(time.time())
    if auth_time <= 0 or now_epoch - auth_time > RECENT_AUTH_SECONDS:
        raise AppError("RECENT_AUTH_REQUIRED", "Please reauthenticate before making this security-sensitive change.", 401)
    return decoded


def require_verified_email() -> None:
    p = current_principal()
    if not p.get("email_verified"):
        raise AppError("EMAIL_VERIFICATION_REQUIRED", "Please verify your email before continuing.", 403)


# =============================================================================
# Security headers
# =============================================================================

_LOCAL_STATIC_URL_RE = re.compile(rb"https?:(?:\\?/){2}(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?((?:\\?/)static(?:\\?/))")


@app.after_request
def rewrite_local_static_urls(response: Response):
    """Media saved while running locally stores absolute localhost URLs; serve them as same-site paths."""
    if response.direct_passthrough or response.is_streamed:
        return response
    mimetype = response.mimetype or ""
    if mimetype not in {"text/html", "application/json"}:
        return response
    body = response.get_data()
    if b"127.0.0.1" not in body and b"localhost" not in body and b"0.0.0.0" not in body:
        return response
    new_body = _LOCAL_STATIC_URL_RE.sub(rb"\1", body)
    if new_body != body:
        response.set_data(new_body)
    return response


@app.after_request
def security_headers(response: Response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
    if CSP_POLICY:
        response.headers.setdefault("Content-Security-Policy", CSP_POLICY)
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Cache-Control", "no-store" if request.path.startswith(("/api/auth", "/profile", "/checkout")) else "no-cache")
    return response


# =============================================================================
# Health / CSRF / template helpers
# =============================================================================

@app.get("/health")
@limiter.exempt
def health():
    status = "ok" if DB is not None else "degraded"
    payload = {
        "status": status,
        "environment": APP_ENV,
        "firebase": "ready" if DB is not None else "unavailable",
        "business_timezone": BUSINESS_TIMEZONE_NAME,
    }
    if DB is None:
        payload["configuration_problem"] = "Firebase/Firestore initialization failed. Check server configuration."
    return api_success(payload, 200 if DB is not None else 503)


@app.get("/api/csrf-token")
@limiter.limit("60 per minute")
def csrf_token_api():
    return api_success({"csrf_token": generate_csrf()})


def public_firebase_web_config() -> dict[str, str]:
    """Return only browser-safe Firebase Web SDK configuration values."""
    config = {
        "apiKey": FIREBASE_WEB_API_KEY,
        "authDomain": FIREBASE_WEB_AUTH_DOMAIN,
        "projectId": FIREBASE_PROJECT_ID,
        "appId": FIREBASE_WEB_APP_ID,
        "messagingSenderId": FIREBASE_WEB_MESSAGING_SENDER_ID,
        "storageBucket": FIREBASE_WEB_STORAGE_BUCKET,
    }
    return {key: value for key, value in config.items() if value}


def render_page(template: str, **context: Any):
    return render_template(
        template,
        business_name=BUSINESS_NAME,
        business_phone=BUSINESS_PHONE,
        principal=getattr(g, "principal", None),
        firebase_web_config=public_firebase_web_config(),
        auth_providers={
            "google_enabled": FIREBASE_GOOGLE_AUTH_ENABLED,
            "facebook_enabled": FIREBASE_FACEBOOK_AUTH_ENABLED,
        },
        **context,
    )


# =============================================================================
# Authentication routes
# =============================================================================

@app.get("/login")
def login_page():
    if getattr(g, "principal", None):
        role = g.principal.get("role")
        if role == ROLE_OWNER:
            return redirect(url_for("owner_dashboard"))
        if role == ROLE_SUPER_ADMIN:
            return redirect(url_for("super_admin_dashboard"))
        if role == ROLE_ADMIN:
            return redirect(url_for("admin_dashboard"))
        if role == ROLE_CHEF:
            return redirect(url_for("chef_dashboard"))
        return redirect(url_for("profile_page"))
    return render_page("login.html")


@app.get("/signup")
def signup_page():
    if getattr(g, "principal", None):
        return redirect(url_for("profile_page"))
    return render_page("signup.html")


@app.post("/auth/session")
@limiter.limit("20 per minute")
def create_auth_session():
    data = payload_data()
    id_token = str(data.get("id_token") or "").strip()

    try:
        decoded, user_record = verify_firebase_id_token(id_token, check_revoked=True)
        profile = ensure_customer_profile(decoded, user_record)
        if profile.get("status", "active") != "active":
            raise AppError("ACCOUNT_DISABLED", "This account is disabled.", 403)

        uid = user_record.uid
        session.clear()
        session.permanent = True
        session["uid"] = uid
        session["auth_time"] = int(decoded.get("auth_time") or time.time())
        session["provider"] = allowed_provider(decoded)
        session["email_verified"] = bool(decoded.get("email_verified", user_record.email_verified))
        session["session_nonce"] = secrets.token_urlsafe(24)

        principal = resolve_principal(uid)
        g.principal = principal.as_dict()
        audit_log("auth.session_created", "user", uid, {"provider": session["provider"]}, actor=principal.as_dict())
        logger.info("Secure auth session created for uid=%s role=%s.", uid, principal.role)
        return api_success({"user": principal.as_dict()}, 201)
    except AppError as exc:
        # Never leave a partially-created Flask session behind when a trusted
        # role/profile check fails.
        session.clear()
        logger.warning("Secure auth session creation failed: %s", exc.code)
        raise


@app.post("/api/auth/profile")
@limiter.limit("20 per minute")
@login_required
def complete_customer_profile():
    p = current_principal()
    data = payload_data()
    if p["role"] != ROLE_CUSTOMER:
        raise AppError("CUSTOMER_ONLY", "Only customer profile fields can be completed here.", 403)

    updates: dict[str, Any] = {
        "full_name": validate_name(data.get("full_name"), "full_name", required=True),
        "phone": validate_phone(data.get("phone"), required=False),
        "city": validate_text(data.get("city"), "city", max_len=100),
        "area": validate_text(data.get("area"), "area", max_len=120),
        "address": validate_text(data.get("address"), "address", max_len=400),
        "marketing_notifications_enabled": validate_bool(data.get("marketing_notifications_enabled"), False),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if "latitude" in data or "longitude" in data:
        updates["latitude"] = validate_latitude(data.get("latitude"), False)
        updates["longitude"] = validate_longitude(data.get("longitude"), False)
    require_db().collection("users").document(p["uid"]).set(updates, merge=True)
    audit_log("customer.profile_completed", "user", p["uid"])
    return api_success(get_document("users", p["uid"], required=True))


@app.get("/forgot-password")
def forgot_password_page():
    return render_page("forgot_password.html")


@app.post("/api/auth/forgot-password")
@limiter.limit("5 per 15 minutes")
def forgot_password_request():
    # Always neutral to prevent account enumeration.
    data = payload_data()
    try:
        email = validate_email(data.get("email"))
    except AppError:
        # Keep public response neutral even for malformed-but-present addresses.
        email = ""
    if email:
        try:
            send_password_reset_email_server_side(email)
        except Exception:
            logger.exception("Password reset request could not be processed.")
    return api_success(
        {
            "message": "If an account exists with this email, password reset instructions have been sent."
        }
    )


@app.get("/reset-password")
@limiter.limit("30 per hour")
def reset_password_page():
    oob_code = request.args.get("oobCode", "").strip()
    mode = request.args.get("mode", "resetPassword").strip()
    if mode != "resetPassword":
        raise AppError("INVALID_RESET_MODE", "Invalid password reset action.", 400)
    if not oob_code:
        return render_page("reset_password.html", reset_valid=False, reset_error="missing_code")
    try:
        result = identity_toolkit_reset_password({"oobCode": oob_code})
        return render_page(
            "reset_password.html",
            reset_valid=True,
            reset_email=result.get("email", ""),
            oob_code=oob_code,
        )
    except AppError as exc:
        return render_page("reset_password.html", reset_valid=False, reset_error=exc.code)


@app.post("/api/auth/reset-password")
@limiter.limit("5 per 15 minutes")
def reset_password_submit():
    data = payload_data()
    oob_code = validate_text(data.get("oob_code") or data.get("oobCode"), "oob_code", required=True, max_len=2048)
    new_password = validate_new_password(data.get("new_password"))
    result = identity_toolkit_reset_password({"oobCode": oob_code, "newPassword": new_password})
    email = str(result.get("email") or "")
    # Firebase invalidates/updates relevant auth state. No password is stored in Firestore.
    return api_success({"message": "Password reset successfully. Please sign in with your new password.", "email": email})


@app.get("/change-password")
@login_required
def change_password_page():
    return render_page("change_password.html")


@app.post("/api/auth/change-password")
@limiter.limit("5 per 15 minutes")
@login_required
def change_password_api():
    p = current_principal()
    data = payload_data()
    recent_token = str(data.get("recent_id_token") or "").strip()
    new_password = validate_new_password(data.get("new_password"))
    require_recent_firebase_token(recent_token, p["uid"])
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        auth.update_user(p["uid"], password=new_password, app=FIREBASE_APP)
        auth.revoke_refresh_tokens(p["uid"], app=FIREBASE_APP)
    except Exception as exc:
        logger.exception("Firebase password change failed.")
        raise AppError("PASSWORD_CHANGE_FAILED", "Password could not be changed.", 400) from exc
    audit_log("auth.password_changed", "user", p["uid"])
    session.clear()
    return api_success({"message": "Password changed. Please sign in again on your devices."})


@app.post("/api/auth/send-email-verification")
@limiter.limit("3 per 30 minutes")
@login_required
def send_email_verification_api():
    p = current_principal()
    if p.get("email_verified"):
        return api_success({"message": "Email is already verified."})
    email = validate_email(p.get("email"))
    delivered = send_verification_email_server_side(email)
    if not smtp_configured():
        return api_success(
            {
                "message": "Server email delivery is not configured. The frontend may use Firebase Client SDK sendEmailVerification after reauthentication.",
                "delivered": False,
            }
        )
    return api_success({"message": "Verification email request processed.", "delivered": delivered})


@app.post("/api/auth/refresh-identity")
@limiter.limit("30 per minute")
@login_required
def refresh_identity_api():
    p = current_principal()
    data = payload_data()
    decoded, user_record = verify_firebase_id_token(str(data.get("id_token") or "").strip(), check_revoked=True)
    token_uid = decoded.get("uid") or decoded.get("sub")
    if token_uid != p["uid"]:
        raise AppError("AUTH_IDENTITY_MISMATCH", "Identity mismatch.", 401)
    require_db().collection("users").document(p["uid"]).set(
        {
            "email": (user_record.email or decoded.get("email") or "").lower(),
            "email_verified": bool(decoded.get("email_verified", user_record.email_verified)),
            "auth_provider": allowed_provider(decoded),
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    session["auth_time"] = int(decoded.get("auth_time") or session.get("auth_time") or 0)
    session["provider"] = allowed_provider(decoded)
    session["email_verified"] = bool(decoded.get("email_verified", user_record.email_verified))
    principal = resolve_principal(p["uid"])
    g.principal = principal.as_dict()
    return api_success({"user": principal.as_dict()})


@app.post("/api/auth/logout")
@login_required
def logout_api():
    uid = current_principal()["uid"]
    audit_log("auth.logout", "user", uid)
    session.clear()
    return api_success({"message": "Signed out."})


@app.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "GET":
        # GET never mutates authentication state; the future frontend should POST with CSRF.
        if wants_json_response():
            return api_error("LOGOUT_REQUIRES_POST", "Use POST /logout to sign out.", 405)
        return render_page("login.html", logout_requires_post=True)
    uid = session.get("uid")
    if uid:
        audit_log("auth.logout", "user", uid)
    session.clear()
    if wants_json_response():
        return api_success({"message": "Signed out."})
    return redirect(url_for("login_page"))


@app.post("/api/auth/logout-all-devices")
@limiter.limit("5 per hour")
@login_required
def logout_all_devices():
    p = current_principal()
    data = payload_data()
    require_recent_firebase_token(str(data.get("recent_id_token") or ""), p["uid"])
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    auth.revoke_refresh_tokens(p["uid"], app=FIREBASE_APP)
    audit_log("auth.refresh_tokens_revoked", "user", p["uid"])
    session.clear()
    return api_success({"message": "All Firebase refresh tokens were revoked. Please sign in again."})


# =============================================================================
# Customer profile
# =============================================================================

@app.get("/profile")
@login_required
def profile_page():
    p = current_principal()
    profile = get_document("users", p["uid"], required=True)
    return render_page("profile.html", profile=profile)


@app.get("/api/profile")
@login_required
def profile_api_get():
    p = current_principal()
    return api_success(get_document("users", p["uid"], required=True))


@app.patch("/api/profile")
@limiter.limit("30 per hour")
@login_required
def profile_api_update():
    p = current_principal()
    data = payload_data()
    allowed: dict[str, Any] = {}
    if "full_name" in data:
        allowed["full_name"] = validate_name(data.get("full_name"), "full_name", True)
    if "phone" in data:
        allowed["phone"] = validate_phone(data.get("phone"), False)
    for field, max_len in (("city", 100), ("area", 120), ("address", 400)):
        if field in data:
            allowed[field] = validate_text(data.get(field), field, max_len=max_len)
    if "latitude" in data:
        allowed["latitude"] = validate_latitude(data.get("latitude"), False)
    if "longitude" in data:
        allowed["longitude"] = validate_longitude(data.get("longitude"), False)
    if "marketing_notifications_enabled" in data:
        allowed["marketing_notifications_enabled"] = validate_bool(data.get("marketing_notifications_enabled"))
    if not allowed:
        raise AppError("NO_CHANGES", "No permitted profile fields were supplied.", 400)
    allowed["updated_at"] = firestore.SERVER_TIMESTAMP
    require_db().collection("users").document(p["uid"]).set(allowed, merge=True)
    audit_log("customer.profile_updated", "user", p["uid"], {"fields": sorted(k for k in allowed if k != "updated_at")})
    return api_success(get_document("users", p["uid"], required=True))


# =============================================================================
# Service selection / fulfilment helpers
# =============================================================================

def validate_service_mode(value: Any) -> str:
    mode = str(value or "").strip().lower().replace("-", "_")
    if mode not in SERVICE_MODES:
        raise AppError("INVALID_SERVICE_MODE", "Service mode must be delivery, pickup, or dine-in.", 422)
    return mode


def get_active_location(location_id: str, service_mode: Optional[str] = None) -> dict[str, Any]:
    location_id = validate_id(location_id, "location_id")
    location = get_document("locations", location_id, required=True, code="LOCATION_NOT_FOUND")
    if not location.get("active", False):
        raise AppError("OUTLET_UNAVAILABLE", "Selected outlet is not active.", 409)
    if service_mode:
        flag = {
            "delivery": "delivery_enabled",
            "pickup": "pickup_enabled",
            "dine_in": "dine_in_enabled",
        }[service_mode]
        if not location.get(flag, False):
            raise AppError("SERVICE_UNAVAILABLE_AT_OUTLET", f"{service_mode.replace('_', ' ').title()} is not enabled at this outlet.", 409)
    return location


def determine_delivery_outlet(
    *,
    requested_outlet_id: str = "",
    city: str = "",
    area: str = "",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> dict[str, Any]:
    if requested_outlet_id:
        return get_active_location(requested_outlet_id, "delivery")

    outlets = filtered_documents(
        "locations",
        [("active", "==", True), ("delivery_enabled", "==", True)],
        limit=50,
    )
    if not outlets:
        raise AppError("NO_DELIVERY_OUTLET", "No active delivery outlet is configured.", 409)

    if latitude is not None and longitude is not None:
        candidates: list[tuple[float, dict[str, Any]]] = []
        for outlet in outlets:
            olat, olon = outlet.get("latitude"), outlet.get("longitude")
            if olat is None or olon is None:
                continue
            distance = haversine_km(latitude, longitude, float(olat), float(olon))
            radius = outlet.get("delivery_radius_km")
            if radius is not None and distance > float(radius):
                continue
            candidates.append((distance, outlet))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            selected = candidates[0][1]
            selected["distance_km"] = round(candidates[0][0], 2)
            return selected

    city_n, area_n = normalize_search(city), normalize_search(area)
    area_matches = []
    for outlet in outlets:
        outlet_city = normalize_search(str(outlet.get("city") or ""))
        delivery_areas = {normalize_search(str(x)) for x in outlet.get("delivery_areas", [])}
        if city_n and outlet_city and city_n != outlet_city:
            continue
        if area_n and delivery_areas and area_n not in delivery_areas:
            continue
        area_matches.append(outlet)
    if len(area_matches) == 1:
        return area_matches[0]
    if area_matches:
        return sorted(area_matches, key=lambda x: int(x.get("display_order", 0)))[0]
    raise AppError(
        "DELIVERY_OUTLET_REQUIRED",
        "Could not determine a delivery outlet from this address. Please select an outlet or provide location coordinates.",
        422,
    )


@app.post("/api/service-selection")
@limiter.limit("60 per hour")
def save_service_selection():
    data = payload_data()
    mode = validate_service_mode(data.get("service_mode"))
    selection: dict[str, Any] = {"service_mode": mode}
    if mode in {"pickup", "dine_in"}:
        outlet = get_active_location(str(data.get("outlet_id") or ""), mode)
        selection["outlet_id"] = outlet["id"]
    else:
        lat = validate_latitude(data.get("latitude"), False)
        lon = validate_longitude(data.get("longitude"), False)
        city = validate_text(data.get("city"), "city", max_len=100)
        area = validate_text(data.get("area"), "area", max_len=120)
        outlet = determine_delivery_outlet(
            requested_outlet_id=str(data.get("outlet_id") or ""),
            city=city,
            area=area,
            latitude=lat,
            longitude=lon,
        )
        selection.update(
            {
                "outlet_id": outlet["id"],
                "city": city,
                "area": area,
                "latitude": lat,
                "longitude": lon,
            }
        )
    session["service_selection"] = selection
    return api_success(selection)


# =============================================================================
# Hero media helpers
# =============================================================================

def hero_is_current(item: dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    if not item.get("active", False):
        return False
    start = item.get("start_at")
    end = item.get("end_at")
    if isinstance(start, datetime) and start > now:
        return False
    if isinstance(end, datetime) and end < now:
        return False
    return True


def active_hero_media() -> list[dict[str, Any]]:
    items = ordered_documents(
        "hero_media",
        [("active", "==", True)],
        order_field="display_order",
        direction="asc",
        limit=30,
    )
    return [x for x in items if hero_is_current(x)]


# =============================================================================
# Public information routes
# =============================================================================

STATIC_INFO_ROUTES = {
    # One reusable information template serves the informational endpoints.
    "/about": ("about_page", "info_page.html"),
    "/our-team": ("our_team_page", "info_page.html"),
    "/ingredients": ("ingredients_page", "info_page.html"),
    "/careers": ("careers_page", "info_page.html"),
    "/support": ("support_page", "info_page.html"),
    "/birthday-parties": ("birthday_parties_page", "info_page.html"),
    "/corporate-orders": ("corporate_orders_page", "info_page.html"),
    "/franchise": ("franchise_page", "info_page.html"),
    "/faq": ("faq_page", "info_page.html"),
    "/privacy": ("privacy_page", "info_page.html"),
    "/terms": ("terms_page", "info_page.html"),
    # Rewards has its own customer-aware template.
    "/rewards": ("rewards_page", "rewards.html"),
}


def _register_static_info_routes() -> None:
    for path, (endpoint, template) in STATIC_INFO_ROUTES.items():
        def view(template_name: str = template):
            settings = get_document("settings", "public_content") if DB is not None else None
            return render_page(template_name, content=settings or {})
        app.add_url_rule(path, endpoint, view, methods=["GET"])


_register_static_info_routes()


@app.get("/")
def home_page():
    if DB is None:
        return render_page("index.html", categories=[], deals=[], featured_products=[], locations=[], hero_media=[])
    categories = ordered_documents(
        "categories", [("active", "==", True)], order_field="display_order", direction="asc", limit=30
    )
    now = utcnow()
    deals = [
        x
        for x in ordered_documents(
            "deals",
            [("active", "==", True), ("featured", "==", True)],
            order_field="display_order",
            direction="asc",
            limit=20,
        )
        if deal_is_current(x, now)
    ]
    featured = ordered_documents(
        "products",
        [("active", "==", True), ("available", "==", True), ("featured", "==", True)],
        order_field="display_order",
        direction="asc",
        limit=20,
    )
    locations = ordered_documents(
        "locations", [("active", "==", True)], order_field="display_order", direction="asc", limit=30
    )
    return render_page(
        "index.html",
        categories=[x for x in categories if x],
        deals=deals,
        featured_products=[x for x in featured if x],
        locations=[x for x in locations if x],
        hero_media=active_hero_media(),
    )


# =============================================================================
# Categories / products / menu
# =============================================================================

def product_available_at_outlet(product: dict[str, Any], outlet_id: Optional[str]) -> bool:
    if not product.get("active", False) or not product.get("available", False):
        return False
    if not outlet_id:
        return True
    availability_map = product.get("availability_by_outlet") or {}
    if isinstance(availability_map, dict) and outlet_id in availability_map:
        return bool(availability_map[outlet_id])
    outlet_ids = product.get("outlet_ids")
    if isinstance(outlet_ids, list) and outlet_ids:
        return outlet_id in outlet_ids
    return True


def get_product(product_id: str, *, require_available: bool = False, outlet_id: Optional[str] = None) -> dict[str, Any]:
    product_id = validate_id(product_id, "product_id")
    product = get_document("products", product_id, required=True, code="PRODUCT_NOT_FOUND")
    if require_available and not product_available_at_outlet(product, outlet_id):
        raise AppError("PRODUCT_UNAVAILABLE", "This product is currently unavailable at the selected outlet.", 409)
    return product


def get_product_option_groups(product_id: str) -> list[dict[str, Any]]:
    return ordered_documents(
        "product_options",
        [("product_id", "==", product_id), ("active", "==", True)],
        order_field="display_order",
        direction="asc",
        limit=50,
    )


def public_review_summary(product_id: str) -> dict[str, Any]:
    reviews = ordered_documents(
        "reviews",
        [("product_id", "==", product_id), ("moderation_state", "==", "approved")],
        order_field="created_at",
        direction="desc",
        limit=200,
    )
    ratings = [int(item.get("rating", 0)) for item in reviews]
    valid = [r for r in ratings if 1 <= r <= 5]
    return {
        "average_rating": round(sum(valid) / len(valid), 2) if valid else 0,
        "review_count": len(valid),
    }


def category_with_count(category: dict[str, Any], outlet_id: Optional[str] = None) -> dict[str, Any]:
    filters = [
        ("category_id", "==", category["id"]),
        ("active", "==", True),
        ("available", "==", True),
    ]
    if outlet_id or ALLOW_FIRESTORE_INDEX_FALLBACK:
        # In development/demo mode, keep the category count resilient on a fresh
        # Firestore project without requiring a composite index just to render Menu.
        products = filtered_documents("products", filters, limit=min(FIRESTORE_FALLBACK_SCAN_LIMIT, 1000))
        count = sum(1 for product in products if product_available_at_outlet(product, outlet_id))
    else:
        count = count_query(firestore_query("products", filters))
    result = dict(category)
    result["item_count"] = count
    return result


def menu_query_products(
    *,
    category_id: Optional[str] = None,
    outlet_id: Optional[str] = None,
    search: str = "",
    popular: Optional[bool] = None,
    featured: Optional[bool] = None,
    spicy: Optional[bool] = None,
    vegetarian: Optional[bool] = None,
    min_price_minor: Optional[int] = None,
    max_price_minor: Optional[int] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    filters: list[tuple[str, str, Any]] = [("active", "==", True), ("available", "==", True)]
    if category_id:
        filters.append(("category_id", "==", category_id))
    for field, value in (("popular", popular), ("featured", featured), ("spicy", spicy), ("vegetarian", vegetarian)):
        if value is not None:
            filters.append((field, "==", value))
    if min_price_minor is not None:
        filters.append(("base_price_minor", ">=", min_price_minor))
    if max_price_minor is not None:
        filters.append(("base_price_minor", "<=", max_price_minor))

    search_n = normalize_search(search)
    max_docs = min(limit, 250)

    try:
        if search_n:
            # Firestore is not a full-text engine. Combine a normalized-name prefix
            # query with an exact keyword query, then de-duplicate results.
            prefix_q = firestore_query("products", filters).order_by("name_search").start_at([search_n]).end_at([search_n + "\uf8ff"])
            keyword_q = firestore_query("products", filters + [("search_keywords", "array_contains", search_n)]).order_by(
                "display_order", direction=firestore.Query.ASCENDING
            )
            merged: dict[str, dict[str, Any]] = {}
            for snap in list(prefix_q.limit(max_docs).stream()) + list(keyword_q.limit(max_docs).stream()):
                item = doc_dict(snap)
                if item:
                    merged[item["id"]] = item
            products = list(merged.values())
        else:
            products = ordered_documents(
                "products", filters, order_field="display_order", direction="asc", limit=max_docs
            )
    except FailedPrecondition as exc:
        if not (_is_missing_firestore_index_error(exc) and ALLOW_FIRESTORE_INDEX_FALLBACK):
            raise
        logger.warning("Missing Firestore catalog index; using bounded development product scan.")
        products = _scan_collection_with_filters("products", filters)

    if search_n:
        def search_matches(product: dict[str, Any]) -> bool:
            name = normalize_search(str(product.get("name") or product.get("name_search") or ""))
            keywords = product.get("search_keywords") or []
            keyword_values = {normalize_search(str(value)) for value in keywords if value is not None} if isinstance(keywords, list) else set()
            return name.startswith(search_n) or search_n in keyword_values or search_n in name

        products = [product for product in products if search_matches(product)]
        products.sort(
            key=lambda p: (
                0 if normalize_search(str(p.get("name", ""))).startswith(search_n) else 1,
                int(p.get("display_order", 0) or 0),
                str(p.get("name", "")).casefold(),
            )
        )
    else:
        products.sort(key=lambda p: (int(p.get("display_order", 0) or 0), str(p.get("name", "")).casefold()))

    products = [p for p in products if product_available_at_outlet(p, outlet_id)]
    return products[:max_docs]


@app.get("/menu")
def menu_page():
    outlet_id = request.args.get("outlet_id", "").strip() or (session.get("service_selection") or {}).get("outlet_id")
    categories = ordered_documents(
        "categories", [("active", "==", True)], order_field="display_order", direction="asc", limit=50
    )
    categories = [category_with_count(c, outlet_id) for c in categories]
    products = menu_query_products(outlet_id=outlet_id, search=request.args.get("q", ""), limit=200)
    return render_page("menu.html", categories=categories, products=products, outlet_id=outlet_id)


@app.get("/api/menu")
def menu_api():
    outlet_id = request.args.get("outlet_id", "").strip() or None
    category_id = request.args.get("category_id", "").strip() or None
    if outlet_id:
        get_active_location(outlet_id)
    if category_id:
        category = get_document("categories", validate_id(category_id, "category_id"), required=True, code="CATEGORY_NOT_FOUND")
        if not category.get("active", False):
            raise AppError("CATEGORY_NOT_FOUND", "Category is not active.", 404)

    def opt_bool(name: str) -> Optional[bool]:
        if name not in request.args:
            return None
        return validate_bool(request.args.get(name))

    min_price = money_to_minor(request.args["min_price"], "min_price") if "min_price" in request.args else None
    max_price = money_to_minor(request.args["max_price"], "max_price") if "max_price" in request.args else None
    if min_price is not None and max_price is not None and min_price > max_price:
        raise AppError("VALIDATION_ERROR", "min_price cannot exceed max_price.", 422)
    products = menu_query_products(
        category_id=category_id,
        outlet_id=outlet_id,
        search=request.args.get("q", ""),
        popular=opt_bool("popular"),
        featured=opt_bool("featured"),
        spicy=opt_bool("spicy"),
        vegetarian=opt_bool("vegetarian"),
        min_price_minor=min_price,
        max_price_minor=max_price,
        limit=min(query_limit(), 200),
    )
    return api_success({"products": products, "search_mode": "normalized_prefix_plus_keywords"})


@app.get("/menu/<category_slug>")
def category_page(category_slug: str):
    slug = validate_slug(category_slug, "category_slug")
    category = first_document("categories", [("slug", "==", slug), ("active", "==", True)])
    if not category:
        abort(404)
    outlet_id = request.args.get("outlet_id", "").strip() or (session.get("service_selection") or {}).get("outlet_id")
    products = menu_query_products(category_id=category["id"], outlet_id=outlet_id, search=request.args.get("q", ""), limit=200)
    category_view = category_with_count(category, outlet_id)
    return render_page(
        "menu.html",
        categories=[category_view],
        products=products,
        outlet_id=outlet_id,
        selected_category=category_view,
    )


@app.get("/product/<product_id>")
def product_page(product_id: str):
    outlet_id = request.args.get("outlet_id", "").strip() or (session.get("service_selection") or {}).get("outlet_id")
    product = get_product(product_id, require_available=False, outlet_id=outlet_id)
    # Template compatibility: older product.html versions read product.images,
    # while the gallery backend stores ordered extras in product.gallery_images.
    # These aliases exist only in the rendered response and do not rewrite Firestore.
    product = dict(product)
    product["gallery_images"] = product_gallery_extra_images(product)
    product["images"] = list(product["gallery_images"])
    category = get_document("categories", product.get("category_id", "")) if product.get("category_id") else None
    options = get_product_option_groups(product["id"])
    summary = public_review_summary(product["id"])
    reviews = ordered_documents(
        "reviews",
        [("product_id", "==", product["id"]), ("moderation_state", "==", "approved")],
        order_field="created_at",
        direction="desc",
        limit=20,
    )
    return render_page(
        "product.html",
        product=product,
        category=category,
        gallery_images=product_gallery_images(product),
        option_groups=options,
        review_summary=summary,
        reviews=reviews,
        outlet_available=product_available_at_outlet(product, outlet_id),
    )


@app.get("/api/products/<product_id>")
def product_api(product_id: str):
    outlet_id = request.args.get("outlet_id", "").strip() or None
    product = get_product(product_id)
    product = dict(product)
    product["gallery_images"] = product_gallery_extra_images(product)
    product["images"] = list(product["gallery_images"])
    category = get_document("categories", product.get("category_id", "")) if product.get("category_id") else None
    return api_success(
        {
            "product": product,
            "category": category,
            "gallery_images": product_gallery_images(product),
            "option_groups": get_product_option_groups(product["id"]),
            "availability": product_available_at_outlet(product, outlet_id),
            "review_summary": public_review_summary(product["id"]),
        }
    )


# =============================================================================
# Deals
# =============================================================================

def deal_is_current(deal: dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    if not deal.get("active", False):
        return False
    start = deal.get("start_at")
    end = deal.get("end_at")
    if isinstance(start, datetime) and start > now:
        return False
    if isinstance(end, datetime) and end < now:
        return False
    return True


def deal_available_at_outlet(deal: dict[str, Any], outlet_id: Optional[str]) -> bool:
    if not deal_is_current(deal):
        return False
    if not outlet_id:
        return True
    availability = deal.get("availability_by_outlet") or {}
    if isinstance(availability, dict) and outlet_id in availability:
        return bool(availability[outlet_id])
    outlet_ids = deal.get("outlet_ids")
    if isinstance(outlet_ids, list) and outlet_ids:
        return outlet_id in outlet_ids
    return True


def get_deal(deal_id: str, *, require_available: bool = False, outlet_id: Optional[str] = None) -> dict[str, Any]:
    deal = get_document("deals", validate_id(deal_id, "deal_id"), required=True, code="DEAL_NOT_FOUND")
    if require_available and not deal_available_at_outlet(deal, outlet_id):
        raise AppError("DEAL_UNAVAILABLE", "This deal is unavailable or has expired.", 409)
    return deal


@app.get("/deals")
def deals_page():
    outlet_id = request.args.get("outlet_id", "").strip() or (session.get("service_selection") or {}).get("outlet_id")
    deals = [
        deal
        for deal in ordered_documents(
            "deals", [("active", "==", True)], order_field="display_order", direction="asc", limit=100
        )
        if deal_available_at_outlet(deal, outlet_id)
    ]
    return render_page("deals.html", deals=deals, outlet_id=outlet_id)


@app.get("/deal/<deal_id>")
def deal_detail_page(deal_id: str):
    outlet_id = request.args.get("outlet_id", "").strip() or (session.get("service_selection") or {}).get("outlet_id")
    deal = get_deal(deal_id)
    return render_page("details.html", deal=deal, outlet_available=deal_available_at_outlet(deal, outlet_id))


# =============================================================================
# Locations
# =============================================================================

@app.get("/locations")
def locations_page():
    locations = ordered_documents(
        "locations", [("active", "==", True)], order_field="display_order", direction="asc", limit=100
    )
    return render_page("locations.html", locations=locations)


@app.get("/location/<location_id>")
def location_detail_page(location_id: str):
    location = get_active_location(location_id)
    return render_page("location.html", location=location)


@app.get("/api/locations")
def locations_api():
    service_mode = request.args.get("service_mode", "").strip().replace("-", "_")
    filters: list[tuple[str, str, Any]] = [("active", "==", True)]
    if service_mode:
        mode = validate_service_mode(service_mode)
        field = {"delivery": "delivery_enabled", "pickup": "pickup_enabled", "dine_in": "dine_in_enabled"}[mode]
        filters.append((field, "==", True))
    locations = ordered_documents(
        "locations", filters, order_field="display_order", direction="asc", limit=100
    )
    return api_success({"locations": locations})


# =============================================================================
# Product customization pricing
# =============================================================================

def normalize_selections(value: Any) -> dict[str, list[str]]:
    if value is None or value == "":
        return {}
    if not isinstance(value, dict):
        raise AppError("INVALID_OPTIONS", "Product selections must be an object keyed by option group.", 422)
    result: dict[str, list[str]] = {}
    for raw_group, raw_selected in value.items():
        group_key = validate_id(raw_group, "option_group")
        if raw_selected is None:
            selected: list[Any] = []
        elif isinstance(raw_selected, list):
            selected = raw_selected
        else:
            selected = [raw_selected]
        if len(selected) > 30:
            raise AppError("INVALID_OPTIONS", "Too many options selected in one group.", 422)
        result[group_key] = list(dict.fromkeys(validate_id(x, "option_id") for x in selected))
    return result


def price_product_configuration(
    product: dict[str, Any], selections: dict[str, list[str]]
) -> tuple[int, list[dict[str, Any]]]:
    """Return the authoritative configured unit price and safe option snapshots.

    Pizza size is a final-price choice, not a delivery fee and not an "extra
    charge". A selected size replaces the product base used for pricing. Normal
    non-size option groups (for example crust/toppings) continue to add their
    configured price deltas on top of that selected size price.

    For backwards compatibility, old size records that only have
    price_delta_minor are interpreted as base_price + price_delta_minor.
    """
    if "base_price_minor" not in product:
        raise ConfigurationError("PRODUCT_PRICE_MISSING", f"Product {product['id']} has no authoritative base price.")

    base_price = int(product["base_price_minor"])
    if base_price < 0:
        raise ConfigurationError("PRODUCT_PRICE_INVALID", f"Product {product['id']} has an invalid base price.")

    groups = get_product_option_groups(product["id"])
    known_group_keys = {str(group.get("group_key") or group.get("id")) for group in groups}
    unknown_groups = set(selections) - known_group_keys
    if unknown_groups:
        raise AppError("INVALID_OPTION_GROUP", "One or more product option groups are invalid.", 422)

    addon_delta_minor = 0
    selected_size_final_minor: Optional[int] = None
    selection_snapshots: list[dict[str, Any]] = []

    for group in groups:
        group_key = str(group.get("group_key") or group["id"])
        group_name = str(group.get("name") or group_key)
        selected_ids = selections.get(group_key, [])
        required = bool(group.get("required", False))
        min_sel = int(group.get("min_selections", 1 if required else 0))
        max_sel = int(group.get("max_selections", 1))

        if len(selected_ids) < min_sel:
            raise AppError(
                "OPTION_SELECTION_REQUIRED",
                f"Select at least {min_sel} option(s) for {group.get('name', group_key)}.",
                422,
            )
        if max_sel >= 0 and len(selected_ids) > max_sel:
            raise AppError(
                "TOO_MANY_OPTIONS",
                f"Select at most {max_sel} option(s) for {group.get('name', group_key)}.",
                422,
            )

        option_map: dict[str, dict[str, Any]] = {}
        for option in group.get("options", []) or []:
            if not isinstance(option, dict):
                continue
            oid = str(option.get("id") or "")
            if oid and option.get("active", True):
                option_map[oid] = option

        size_group = (
            str(group.get("pricing_mode") or "").strip().lower() == "final_size_price"
            or _is_size_option_group(group_key, group_name)
        )

        for selected_id in selected_ids:
            option = option_map.get(selected_id)
            if not option:
                raise AppError("INVALID_OPTION", f"Selected option {selected_id} is unavailable.", 422)

            stored_delta = int(option.get("price_delta_minor", 0) or 0)

            if size_group:
                if selected_size_final_minor is not None:
                    raise ConfigurationError(
                        "MULTIPLE_SIZE_GROUPS",
                        f"Product {product['id']} has more than one active pizza-size selection group.",
                    )

                raw_final = option.get("final_price_minor")
                final_price_minor = (
                    base_price + stored_delta
                    if raw_final in (None, "")
                    else int(raw_final)
                )

                if final_price_minor < base_price:
                    raise ConfigurationError(
                        "PRODUCT_SIZE_PRICE_INVALID",
                        f"Product {product['id']} has a size price below its authoritative base price.",
                    )

                selected_size_final_minor = final_price_minor
                selection_snapshots.append(
                    {
                        "group_id": group["id"],
                        "group_key": group_key,
                        "group_name": group_name,
                        "option_id": selected_id,
                        "option_name": option.get("name", selected_id),
                        "pricing_mode": "final_size_price",
                        "final_price_minor": final_price_minor,
                        "price_adjustment_minor": final_price_minor - base_price,
                        "price_delta_minor": 0,
                        "charge_scope": "item_price",
                        "charge_label": "Size price",
                    }
                )
                continue

            addon_delta_minor += stored_delta
            selection_snapshots.append(
                {
                    "group_id": group["id"],
                    "group_key": group_key,
                    "group_name": group_name,
                    "option_id": selected_id,
                    "option_name": option.get("name", selected_id),
                    "pricing_mode": "price_delta",
                    "price_delta_minor": stored_delta,
                    "charge_scope": "item_option",
                    "charge_label": group_name,
                }
            )

    configured_item_price = selected_size_final_minor if selected_size_final_minor is not None else base_price
    unit_price = configured_item_price + addon_delta_minor

    if unit_price < 0:
        raise ConfigurationError("PRODUCT_PRICE_INVALID", "Calculated product price is invalid.")

    return unit_price, selection_snapshots


def cart_item_from_product(
    product: dict[str, Any],
    quantity: int,
    selections: dict[str, list[str]],
) -> dict[str, Any]:
    unit_price, selected_options = price_product_configuration(product, selections)
    line_total = unit_price * quantity
    return {
        "item_id": uuid.uuid4().hex,
        "item_type": "product",
        "product_id": product["id"],
        "product_name": product.get("name", ""),
        "image": product.get("image") or product.get("image_url") or "",
        "quantity": quantity,
        "selections": selections,
        "selected_options": selected_options,
        "base_price_minor": int(product.get("base_price_minor", 0)),
        "unit_price_minor": unit_price,
        "line_total_minor": line_total,
        "pricing_version": int(product.get("pricing_version", 1)),
        "added_at": utcnow(),
    }


def cart_item_from_deal(deal: dict[str, Any], quantity: int) -> dict[str, Any]:
    if "price_minor" not in deal:
        raise ConfigurationError("DEAL_PRICE_MISSING", f"Deal {deal['id']} has no authoritative price.")
    unit_price = int(deal["price_minor"])
    return {
        "item_id": uuid.uuid4().hex,
        "item_type": "deal",
        "deal_id": deal["id"],
        "deal_name": deal.get("name", ""),
        "image": deal.get("image") or deal.get("image_url") or "",
        "included_items": deal.get("included_items", []),
        "quantity": quantity,
        "unit_price_minor": unit_price,
        "line_total_minor": unit_price * quantity,
        "pricing_version": int(deal.get("pricing_version", 1)),
        "added_at": utcnow(),
    }


def get_cart(uid: str) -> dict[str, Any]:
    db = require_db()
    snap = db.collection("carts").document(uid).get()
    if not snap.exists:
        return {
            "id": uid,
            "user_id": uid,
            "items": [],
            "outlet_id": None,
            "service_mode": None,
            "revision": 0,
        }
    data = snap.to_dict() or {}
    data["id"] = uid
    data.setdefault("items", [])
    data.setdefault("revision", 0)
    return data


def cart_totals(cart: dict[str, Any], delivery_fee_minor: int = 0) -> dict[str, Any]:
    subtotal = sum(int(item.get("line_total_minor", 0)) for item in cart.get("items", []))
    return {
        "subtotal_minor": subtotal,
        "delivery_fee_minor": int(delivery_fee_minor),
        "total_minor": subtotal + int(delivery_fee_minor),
        "subtotal": money_fields(subtotal),
        "delivery_fee": money_fields(int(delivery_fee_minor)),
        "total": money_fields(subtotal + int(delivery_fee_minor)),
    }


def _public_cart_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return a presentation-safe cart item without changing Firestore data."""
    out = dict(item)

    if out.get("item_type") != "product":
        return out

    try:
        base_price_minor = int(out.get("base_price_minor") or 0)
    except (TypeError, ValueError):
        base_price_minor = 0

    public_options: list[dict[str, Any]] = []
    for raw_option in out.get("selected_options", []) or []:
        if not isinstance(raw_option, dict):
            continue

        option = dict(raw_option)
        group_key = str(option.get("group_key") or "")
        group_name = str(option.get("group_name") or group_key)
        pricing_mode = str(option.get("pricing_mode") or "").strip().lower()

        size_option = (
            pricing_mode == "final_size_price"
            or _is_size_option_group(group_key, group_name)
        )

        if size_option:
            try:
                old_delta = int(option.get("price_adjustment_minor", option.get("price_delta_minor", 0)) or 0)
            except (TypeError, ValueError):
                old_delta = 0

            raw_final = option.get("final_price_minor")
            try:
                final_price_minor = (
                    int(raw_final)
                    if raw_final not in (None, "")
                    else base_price_minor + old_delta
                )
            except (TypeError, ValueError):
                final_price_minor = base_price_minor + old_delta

            option["pricing_mode"] = "final_size_price"
            option["final_price_minor"] = final_price_minor
            option["price_adjustment_minor"] = final_price_minor - base_price_minor
            option["price_delta_minor"] = 0
            option["charge_scope"] = "item_price"
            option["charge_label"] = "Size price"

        public_options.append(option)

    out["selected_options"] = public_options
    return out


def cart_public_data(cart: dict[str, Any]) -> dict[str, Any]:
    """Return cart data with item subtotal only.

    Delivery is intentionally NOT part of an individual product/deal line and is
    never multiplied by item count. For delivery orders the authoritative fee is
    calculated once for the whole order by /api/checkout/quote and checkout_submit.
    """
    out = dict(cart)
    out["items"] = [
        _public_cart_item(item)
        for item in (cart.get("items", []) or [])
        if isinstance(item, dict)
    ]

    totals = cart_totals(cart, 0)
    totals["other_charges"] = []
    totals["delivery_fee_included"] = False
    totals["delivery_fee_scope"] = "order"
    totals["delivery_fee_status"] = (
        "calculated_at_checkout"
        if str(cart.get("service_mode") or "") == "delivery"
        else "not_applicable"
    )
    out["totals"] = totals
    return out


# =============================================================================
# Cart routes
# =============================================================================

@app.get("/cart")
@login_required
def cart_page():
    p = current_principal()
    cart = get_cart(p["uid"])
    return render_page("cart.html", cart=cart_public_data(cart))


@app.get("/api/cart")
@login_required
def get_cart_api():
    p = current_principal()
    return api_success(cart_public_data(get_cart(p["uid"])))


@app.post("/api/cart/items")
@limiter.limit("120 per hour")
@customer_required
def add_cart_item_api():
    p = current_principal()
    data = payload_data()
    item_type = str(data.get("item_type") or "product").strip().lower()
    quantity = validate_int(data.get("quantity", 1), "quantity", 1, 20)
    mode = validate_service_mode(data.get("service_mode") or (session.get("service_selection") or {}).get("service_mode") or "")
    outlet_id = str(data.get("outlet_id") or (session.get("service_selection") or {}).get("outlet_id") or "").strip()
    outlet = get_active_location(outlet_id, mode)

    if item_type == "product":
        product = get_product(str(data.get("product_id") or ""), require_available=True, outlet_id=outlet["id"])
        selections = normalize_selections(data.get("selections") or {})
        item = cart_item_from_product(product, quantity, selections)
    elif item_type == "deal":
        deal = get_deal(str(data.get("deal_id") or ""), require_available=True, outlet_id=outlet["id"])
        item = cart_item_from_deal(deal, quantity)
    else:
        raise AppError("INVALID_CART_ITEM", "item_type must be product or deal.", 422)

    db = require_db()
    cart_ref = db.collection("carts").document(p["uid"])
    transaction = db.transaction()

    @firestore.transactional
    def add_txn(txn):
        snap = cart_ref.get(transaction=txn)
        cart = snap.to_dict() if snap.exists else {}
        items = list(cart.get("items", []))
        existing_outlet = cart.get("outlet_id")
        existing_mode = cart.get("service_mode")
        if items and existing_outlet and existing_outlet != outlet["id"]:
            raise AppError("CART_OUTLET_MISMATCH", "Clear the cart before ordering from a different outlet.", 409)
        if items and existing_mode and existing_mode != mode:
            raise AppError("CART_SERVICE_MODE_MISMATCH", "Clear the cart before changing service mode.", 409)
        items.append(item)
        revision = int(cart.get("revision", 0)) + 1
        txn.set(
            cart_ref,
            {
                "user_id": p["uid"],
                "items": items,
                "outlet_id": outlet["id"],
                "service_mode": mode,
                "revision": revision,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "created_at": cart.get("created_at", firestore.SERVER_TIMESTAMP),
            },
            merge=True,
        )
        return revision

    add_txn(transaction)
    return api_success(cart_public_data(get_cart(p["uid"])), 201)


@app.patch("/api/cart/items/<item_id>")
@limiter.limit("180 per hour")
@customer_required
def update_cart_item_api(item_id: str):
    p = current_principal()
    item_id = validate_id(item_id, "item_id")
    data = payload_data()
    db = require_db()
    cart_ref = db.collection("carts").document(p["uid"])
    transaction = db.transaction()

    @firestore.transactional
    def update_txn(txn):
        snap = cart_ref.get(transaction=txn)
        if not snap.exists:
            raise AppError("CART_EMPTY", "Your cart is empty.", 409)
        cart = snap.to_dict() or {}
        items = list(cart.get("items", []))
        index = next((i for i, x in enumerate(items) if x.get("item_id") == item_id), None)
        if index is None:
            raise AppError("CART_ITEM_NOT_FOUND", "Cart item was not found.", 404)
        existing = items[index]
        quantity = validate_int(data.get("quantity", existing.get("quantity", 1)), "quantity", 1, 20)
        outlet_id = cart.get("outlet_id")
        if existing.get("item_type") == "product":
            product = get_product(existing.get("product_id", ""), require_available=True, outlet_id=outlet_id)
            selections = normalize_selections(data.get("selections", existing.get("selections", {})))
            replacement = cart_item_from_product(product, quantity, selections)
        else:
            deal = get_deal(existing.get("deal_id", ""), require_available=True, outlet_id=outlet_id)
            replacement = cart_item_from_deal(deal, quantity)
        replacement["item_id"] = item_id
        replacement["added_at"] = existing.get("added_at", utcnow())
        items[index] = replacement
        txn.update(
            cart_ref,
            {
                "items": items,
                "revision": int(cart.get("revision", 0)) + 1,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )

    update_txn(transaction)
    return api_success(cart_public_data(get_cart(p["uid"])))


@app.delete("/api/cart/items/<item_id>")
@customer_required
def remove_cart_item_api(item_id: str):
    p = current_principal()
    item_id = validate_id(item_id, "item_id")
    db = require_db()
    cart_ref = db.collection("carts").document(p["uid"])
    transaction = db.transaction()

    @firestore.transactional
    def remove_txn(txn):
        snap = cart_ref.get(transaction=txn)
        if not snap.exists:
            raise AppError("CART_EMPTY", "Your cart is empty.", 409)
        cart = snap.to_dict() or {}
        old_items = list(cart.get("items", []))
        items = [x for x in old_items if x.get("item_id") != item_id]
        if len(items) == len(old_items):
            raise AppError("CART_ITEM_NOT_FOUND", "Cart item was not found.", 404)
        update = {
            "items": items,
            "revision": int(cart.get("revision", 0)) + 1,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        if not items:
            update.update({"outlet_id": None, "service_mode": None})
        txn.update(cart_ref, update)

    remove_txn(transaction)
    return api_success(cart_public_data(get_cart(p["uid"])))


@app.delete("/api/cart")
@customer_required
def clear_cart_api():
    p = current_principal()
    db = require_db()
    db.collection("carts").document(p["uid"]).set(
        {
            "user_id": p["uid"],
            "items": [],
            "outlet_id": None,
            "service_mode": None,
            "revision": firestore.Increment(1),
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    return api_success(cart_public_data(get_cart(p["uid"])))


# =============================================================================
# Delivery fee and checkout validation
# =============================================================================

def _legacy_delivery_fee(outlet_id: str, city: str, area: str) -> Optional[int]:
    """Compatibility fallback for older area/outlet/default delivery fee settings."""
    settings = get_document("settings", "delivery_fees") or {}
    normalized_area = normalize_search(area)
    normalized_city = normalize_search(city)

    area_fees = settings.get("area_fees") or {}
    if isinstance(area_fees, dict):
        keys = [f"{normalized_city}|{normalized_area}", normalized_area]
        for key in keys:
            if key and key in area_fees:
                return int(area_fees[key])

    outlet_fees = settings.get("outlet_fees") or {}
    if isinstance(outlet_fees, dict) and outlet_id in outlet_fees:
        return int(outlet_fees[outlet_id])

    if settings.get("default_fee_minor") is not None:
        return int(settings["default_fee_minor"])
    if DEFAULT_DELIVERY_FEE_PKR:
        return money_to_minor(DEFAULT_DELIVERY_FEE_PKR, "DEFAULT_DELIVERY_FEE_PKR")
    if ALLOW_ZERO_DELIVERY_FEE:
        return 0
    return None


def _delivery_pricing_tiers(outlet_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return active outlet-specific tiers first, then active global tiers."""
    tiers = _scan_collection_with_filters(
        "delivery_pricing_tiers",
        [("active", "==", True)],
        scan_limit=250,
    )

    def tier_sort_key(item: dict[str, Any]) -> tuple[float, float, int]:
        try:
            maximum = float(item.get("max_distance_km", 0))
        except (TypeError, ValueError):
            maximum = 0.0
        try:
            minimum = float(item.get("min_distance_km", 0))
        except (TypeError, ValueError):
            minimum = 0.0
        return (minimum, maximum, int(item.get("display_order", 0) or 0))

    specific = [x for x in tiers if str(x.get("outlet_id") or "") == outlet_id]
    global_tiers = [x for x in tiers if not str(x.get("outlet_id") or "").strip()]
    specific.sort(key=tier_sort_key)
    global_tiers.sort(key=tier_sort_key)
    return specific, global_tiers


def _distance_tier_matches(distance_km: float, tier: dict[str, Any]) -> bool:
    try:
        minimum = float(tier.get("min_distance_km", 0))
        maximum = float(tier.get("max_distance_km", 0))
    except (TypeError, ValueError):
        return False
    # 0-5 includes 0 and 5. The next 5-10 tier means >5 through 10,
    # so adjacent tiers can touch at their boundary without double charging.
    lower_ok = distance_km >= minimum if minimum <= 0 else distance_km > minimum
    return lower_ok and distance_km <= maximum


def delivery_pricing_quote(
    outlet: dict[str, Any],
    city: str,
    area: str,
    latitude: Optional[float],
    longitude: Optional[float],
) -> dict[str, Any]:
    """Calculate an authoritative delivery quote from outlet/customer coordinates.

    Distance tiers live in the delivery_pricing_tiers collection and can be
    global or outlet-specific. Outlet-specific tiers take precedence.
    """
    outlet_id = validate_id(outlet.get("id"), "outlet_id")
    specific_tiers, global_tiers = _delivery_pricing_tiers(outlet_id)
    has_distance_tiers = bool(specific_tiers or global_tiers)

    if latitude is not None and longitude is not None:
        outlet_lat = outlet.get("latitude")
        outlet_lon = outlet.get("longitude")
        if outlet_lat is None or outlet_lon is None:
            raise ConfigurationError(
                "OUTLET_COORDINATES_MISSING",
                "This outlet needs latitude and longitude before distance-based delivery pricing can be used.",
            )

        distance = haversine_km(
            float(latitude),
            float(longitude),
            float(outlet_lat),
            float(outlet_lon),
        )
        distance_rounded = round(distance, 2)

        radius = outlet.get("delivery_radius_km")
        if radius is not None and distance > float(radius):
            raise AppError(
                "DELIVERY_OUT_OF_RANGE",
                f"This address is {distance_rounded:.2f} km from the selected outlet and is outside its delivery radius.",
                409,
                {"distance_km": distance_rounded, "delivery_radius_km": float(radius)},
            )

        for scope, tiers in (("outlet", specific_tiers), ("global", global_tiers)):
            for tier in tiers:
                if not _distance_tier_matches(distance, tier):
                    continue
                fee_minor = int(tier.get("fee_minor") or 0)
                if fee_minor < 0:
                    raise ConfigurationError(
                        "DELIVERY_FEE_INVALID",
                        "A configured delivery pricing tier contains an invalid fee.",
                    )
                return {
                    "fee_minor": fee_minor,
                    "distance_km": distance_rounded,
                    "tier_id": tier.get("id"),
                    "tier_label": str(tier.get("label") or ""),
                    "source": f"distance_tier_{scope}",
                }

        if has_distance_tiers:
            raise AppError(
                "DELIVERY_OUT_OF_RANGE",
                f"No active delivery price tier covers {distance_rounded:.2f} km for this outlet.",
                409,
                {"distance_km": distance_rounded},
            )

    # If coordinates were not supplied, preserve legacy area/outlet/default
    # configuration when present. This keeps manual-address checkout compatible.
    legacy_fee = _legacy_delivery_fee(outlet_id, city, area)
    if legacy_fee is not None:
        return {
            "fee_minor": int(legacy_fee),
            "distance_km": None,
            "tier_id": None,
            "tier_label": "",
            "source": "legacy",
        }

    if has_distance_tiers and (latitude is None or longitude is None):
        raise AppError(
            "DELIVERY_LOCATION_REQUIRED",
            "Use My Current Location on Checkout so the server can calculate the automatic distance-based delivery charge.",
            422,
        )

    raise ConfigurationError(
        "DELIVERY_FEE_NOT_CONFIGURED",
        "Delivery pricing is not configured for this address/outlet.",
    )


def calculate_delivery_fee(
    outlet_id: str,
    city: str,
    area: str,
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> int:
    """Backward-compatible integer fee helper used by older internal callers."""
    outlet = get_active_location(outlet_id, "delivery")
    return int(delivery_pricing_quote(outlet, city, area, latitude, longitude)["fee_minor"])


def revalidate_cart_items(cart: dict[str, Any]) -> list[dict[str, Any]]:
    outlet_id = cart.get("outlet_id")
    if not outlet_id:
        raise AppError("OUTLET_REQUIRED", "Cart has no selected outlet.", 422)
    validated: list[dict[str, Any]] = []
    for existing in cart.get("items", []):
        quantity = validate_int(existing.get("quantity", 1), "quantity", 1, 20)
        if existing.get("item_type") == "product":
            product = get_product(existing.get("product_id", ""), require_available=True, outlet_id=outlet_id)
            selections = normalize_selections(existing.get("selections") or {})
            item = cart_item_from_product(product, quantity, selections)
            item["item_id"] = existing.get("item_id") or uuid.uuid4().hex
            item["entity_updated_at"] = product.get("updated_at")
        elif existing.get("item_type") == "deal":
            deal = get_deal(existing.get("deal_id", ""), require_available=True, outlet_id=outlet_id)
            item = cart_item_from_deal(deal, quantity)
            item["item_id"] = existing.get("item_id") or uuid.uuid4().hex
            item["entity_updated_at"] = deal.get("updated_at")
        else:
            raise AppError("INVALID_CART_ITEM", "Cart contains an invalid item.", 409)
        validated.append(item)
    if not validated:
        raise AppError("CART_EMPTY", "Your cart is empty.", 409)
    return validated


def validate_checkout_context(data: dict[str, Any], profile: dict[str, Any], cart: dict[str, Any]) -> dict[str, Any]:
    mode = validate_service_mode(data.get("service_mode") or cart.get("service_mode") or "")
    cart_mode = cart.get("service_mode")
    if cart_mode and cart_mode != mode:
        raise AppError("CART_SERVICE_MODE_MISMATCH", "Checkout service mode does not match the cart.", 409)

    name = validate_name(data.get("customer_name") or profile.get("full_name"), "customer_name", True)
    phone = validate_phone(data.get("phone") or profile.get("phone"), True)
    email = validate_email(profile.get("email"), True)
    notes = validate_text(data.get("order_notes"), "order_notes", max_len=500)
    schedule_for = parse_iso_datetime(data.get("schedule_for"), "schedule_for", False)
    if schedule_for and schedule_for < utcnow() + timedelta(minutes=20):
        raise AppError("INVALID_SCHEDULE_TIME", "Scheduled orders must be at least 20 minutes in the future.", 422)

    cart_outlet_id = str(cart.get("outlet_id") or "")
    requested_outlet = str(data.get("outlet_id") or cart_outlet_id).strip()
    if cart_outlet_id and requested_outlet and cart_outlet_id != requested_outlet:
        raise AppError("CART_OUTLET_MISMATCH", "Checkout outlet does not match the cart.", 409)

    context: dict[str, Any] = {
        "service_mode": mode,
        "customer_name": name,
        "email": email,
        "phone": phone,
        "order_notes": notes,
        "schedule_for": schedule_for,
    }

    if mode == "delivery":
        city = validate_text(data.get("city") or profile.get("city"), "city", required=True, max_len=100)
        area = validate_text(data.get("area") or profile.get("area"), "area", required=True, max_len=120)
        address = validate_text(data.get("address") or profile.get("address"), "address", required=True, max_len=400)
        lat = validate_latitude(data.get("latitude", profile.get("latitude")), False)
        lon = validate_longitude(data.get("longitude", profile.get("longitude")), False)
        outlet = determine_delivery_outlet(
            requested_outlet_id=requested_outlet,
            city=city,
            area=area,
            latitude=lat,
            longitude=lon,
        )
        if cart_outlet_id and outlet["id"] != cart_outlet_id:
            raise AppError("CART_OUTLET_MISMATCH", "Delivery address resolves to a different outlet; clear/rebuild the cart.", 409)
        delivery_quote = delivery_pricing_quote(outlet, city, area, lat, lon)
        context.update(
            {
                "outlet": outlet,
                "outlet_id": outlet["id"],
                "address": address,
                "city": city,
                "area": area,
                "latitude": lat,
                "longitude": lon,
                "delivery_fee_minor": int(delivery_quote["fee_minor"]),
                "delivery_distance_km": delivery_quote.get("distance_km"),
                "delivery_pricing_tier_id": delivery_quote.get("tier_id"),
                "delivery_pricing_tier_label": delivery_quote.get("tier_label", ""),
                "delivery_pricing_source": delivery_quote.get("source", ""),
            }
        )
    else:
        outlet = get_active_location(requested_outlet, mode)
        context.update(
            {
                "outlet": outlet,
                "outlet_id": outlet["id"],
                "address": "",
                "city": "",
                "area": "",
                "latitude": None,
                "longitude": None,
                "delivery_fee_minor": 0,
                "delivery_distance_km": None,
                "delivery_pricing_tier_id": None,
                "delivery_pricing_tier_label": "",
                "delivery_pricing_source": "",
            }
        )
    return context


def validate_payment_method(value: Any) -> str:
    method = str(value or "").strip().lower()
    if method not in PAYMENT_METHODS:
        raise AppError("INVALID_PAYMENT_METHOD", "Payment method must be COD, Easypaisa, or bank transfer.", 422)
    return method


def demo_easypaisa_enabled() -> bool:
    """Return True only for the non-production Easypaisa wallet simulator."""
    return bool(DEMO_EASYPAISA_ENABLED and not IS_PRODUCTION)


def payment_method_preflight(method: str) -> None:
    settings = get_document("settings", "payment_methods") or {}
    if method == "cod":
        if not bool(settings.get("cod_enabled", True)):
            raise AppError("PAYMENT_METHOD_UNAVAILABLE", "Cash on Delivery is currently unavailable.", 409)
        return
    if method == "easypaisa":
        if demo_easypaisa_enabled():
            return
        easypaisa_preflight()
        return
    if method == "bank_transfer":
        bank = settings.get("bank_transfer") if isinstance(settings.get("bank_transfer"), dict) else {}
        enabled = bool(bank.get("enabled", False))
        has_destination = bool(str(bank.get("account_number") or "").strip() or str(bank.get("iban") or "").strip())
        if not enabled or not has_destination:
            raise ConfigurationError(
                "BANK_TRANSFER_NOT_CONFIGURED",
                "Bank transfer is selected but verified business bank instructions are not configured.",
            )
        return


def easypaisa_adapter_callable() -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    if not EASYPAISA_ENABLED:
        raise ConfigurationError("EASYPAISA_NOT_CONFIGURED", "Easypaisa payments are not enabled.")
    if not EASYPAISA_ENDPOINT or not EASYPAISA_MERCHANT_ID:
        raise ConfigurationError(
            "EASYPAISA_NOT_CONFIGURED",
            "Official Easypaisa endpoint and merchant identifier must be configured.",
        )
    if not EASYPAISA_ADAPTER or ":" not in EASYPAISA_ADAPTER:
        raise ConfigurationError(
            "EASYPAISA_ADAPTER_MISSING",
            "An official merchant-integration adapter is required before Easypaisa transactions can be initiated or verified.",
        )
    module_name, function_name = EASYPAISA_ADAPTER.rsplit(":", 1)
    try:
        module = importlib.import_module(module_name)
        func = getattr(module, function_name)
    except Exception as exc:
        raise ConfigurationError("EASYPAISA_ADAPTER_INVALID", "Configured Easypaisa adapter could not be loaded.") from exc
    if not callable(func):
        raise ConfigurationError("EASYPAISA_ADAPTER_INVALID", "Configured Easypaisa adapter is not callable.")
    return func


def easypaisa_preflight() -> None:
    easypaisa_adapter_callable()


def easypaisa_call(action: str, context: dict[str, Any]) -> dict[str, Any]:
    adapter = easypaisa_adapter_callable()
    safe_context = {
        **context,
        "endpoint": EASYPAISA_ENDPOINT,
        "merchant_id": EASYPAISA_MERCHANT_ID,
        "timeout_seconds": EASYPAISA_TIMEOUT_SECONDS,
    }
    try:
        result = adapter(action, safe_context)
    except AppError:
        raise
    except Exception as exc:
        logger.exception("Easypaisa adapter failed for action=%s", action)
        raise AppError("PAYMENT_PROVIDER_ERROR", "Easypaisa could not process this request.", 502) from exc
    if not isinstance(result, dict):
        raise AppError("PAYMENT_PROVIDER_ERROR", "Easypaisa adapter returned an invalid response.", 502)
    return result


# =============================================================================
# Checkout / order creation
# =============================================================================

@app.get("/checkout")
@customer_required
def checkout_page():
    p = current_principal()
    profile = get_document("users", p["uid"], required=True)
    cart = get_cart(p["uid"])
    return render_page("checkout.html", profile=profile, cart=cart_public_data(cart))


@app.post("/api/checkout/quote")
@limiter.limit("60 per hour")
@customer_required
def checkout_quote_api():
    """Return an authoritative non-mutating checkout total for the current cart."""
    p = current_principal()
    data = payload_data()
    profile = get_document("users", p["uid"], required=True)
    cart = get_cart(p["uid"])
    if not cart.get("items"):
        raise AppError("CART_EMPTY", "Your cart is empty.", 409)

    context = validate_checkout_context(data, profile, cart)
    validated_items = revalidate_cart_items(cart)
    subtotal_minor = sum(int(x["line_total_minor"]) for x in validated_items)
    delivery_fee_minor = int(context["delivery_fee_minor"])
    total_minor = subtotal_minor + delivery_fee_minor

    return api_success(
        {
            "currency": "PKR",
            "subtotal": money_fields(subtotal_minor),
            "delivery_fee": money_fields(delivery_fee_minor),
            "total": money_fields(total_minor),
            "service_mode": context["service_mode"],
            "outlet_id": context["outlet_id"],
            "distance_km": context.get("delivery_distance_km"),
            "delivery_pricing": {
                "source": context.get("delivery_pricing_source", ""),
                "tier_id": context.get("delivery_pricing_tier_id"),
                "tier_label": context.get("delivery_pricing_tier_label", ""),
                "charge_scope": "order",
                "applies_once_per_order": context["service_mode"] == "delivery",
            },
        }
    )


@app.post("/checkout")
@app.post("/api/checkout")
@limiter.limit("20 per hour")
@customer_required
def checkout_submit():
    p = current_principal()
    data = payload_data()

    # Email verification is OPTIONAL for ordering.
    # Keep the real Firebase email_verified state for account information,
    # but do not block a signed-in active customer from placing an order.
    idempotency_key = validate_idempotency_key(
        request.headers.get("Idempotency-Key") or data.get("idempotency_key")
    )
    payment_method = validate_payment_method(data.get("payment_method"))
    payment_method_preflight(payment_method)  # fail safely before order creation when a selected method is unavailable

    profile = get_document("users", p["uid"], required=True)
    cart = get_cart(p["uid"])
    if not cart.get("items"):
        raise AppError("CART_EMPTY", "Your cart is empty.", 409)
    context = validate_checkout_context(data, profile, cart)

    easypaisa_payer_phone = ""
    if payment_method == "easypaisa":
        easypaisa_payer_phone = normalize_easypaisa_mobile(
            data.get("easypaisa_phone")
            or data.get("easypaisa_customer_phone")
            or data.get("wallet_phone")
            or context.get("phone"),
            required=True,
        )

    validated_items = revalidate_cart_items(cart)
    subtotal_minor = sum(int(x["line_total_minor"]) for x in validated_items)
    delivery_fee_minor = int(context["delivery_fee_minor"])
    total_minor = subtotal_minor + delivery_fee_minor
    if total_minor <= 0:
        raise AppError("INVALID_ORDER_TOTAL", "Order total must be greater than zero.", 422)

    db = require_db()
    idem_id = hash_idempotency(p["uid"], idempotency_key)
    idem_ref = db.collection("order_idempotency").document(idem_id)
    cart_ref = db.collection("carts").document(p["uid"])
    order_id = uuid.uuid4().hex
    order_ref = db.collection("orders").document(order_id)
    payment_id = uuid.uuid4().hex
    payment_ref = db.collection("payments").document(payment_id)
    history_id = uuid.uuid4().hex
    history_ref = db.collection("order_status_history").document(history_id)
    public_ref = generate_public_order_reference(order_id)
    expected_revision = int(cart.get("revision", 0))

    initial_payment_status = {
        "cod": "pending",
        "easypaisa": "pending",
        "bank_transfer": "awaiting_verification",
    }[payment_method]

    transaction = db.transaction()

    @firestore.transactional
    def create_order_txn(txn):
        idem_snap = idem_ref.get(transaction=txn)
        if idem_snap.exists:
            existing = idem_snap.to_dict() or {}
            return {"duplicate": True, "order_id": existing.get("order_id"), "payment_id": existing.get("payment_id")}

        live_cart_snap = cart_ref.get(transaction=txn)
        if not live_cart_snap.exists:
            raise AppError("CART_EMPTY", "Your cart is empty.", 409)
        live_cart = live_cart_snap.to_dict() or {}
        if int(live_cart.get("revision", 0)) != expected_revision:
            raise AppError("CART_CHANGED", "Your cart changed during checkout. Review it and try again.", 409)

        # All transaction reads occur before writes. Validate pricing versions and active state again.
        entity_snaps: list[tuple[dict[str, Any], Any]] = []
        for item in validated_items:
            if item["item_type"] == "product":
                ref = db.collection("products").document(item["product_id"])
            else:
                ref = db.collection("deals").document(item["deal_id"])
            snap = ref.get(transaction=txn)
            entity_snaps.append((item, snap))

        for item, snap in entity_snaps:
            if not snap.exists:
                raise AppError("ITEM_UNAVAILABLE", "An item in your cart no longer exists.", 409)
            entity = snap.to_dict() or {}
            entity_with_id = {**entity, "id": snap.id}
            if item["item_type"] == "product":
                if not product_available_at_outlet(entity_with_id, context["outlet_id"]):
                    raise AppError("PRODUCT_UNAVAILABLE", "A product in your cart became unavailable at this outlet.", 409)
            else:
                if not deal_available_at_outlet(entity_with_id, context["outlet_id"]):
                    raise AppError("DEAL_UNAVAILABLE", "A deal in your cart became unavailable or expired at this outlet.", 409)
            if int(entity.get("pricing_version", 1)) != int(item.get("pricing_version", 1)):
                raise AppError("PRICE_CHANGED", "Pricing changed during checkout. Review your cart and try again.", 409)

        order_doc = {
            "order_id": order_id,
            "order_reference": public_ref,
            "customer_uid": p["uid"],
            "customer_snapshot": {
                "name": context["customer_name"],
                "email": context["email"],
                "phone": context["phone"],
            },
            "service_mode": context["service_mode"],
            "outlet_id": context["outlet_id"],
            "outlet_snapshot": {
                "name": context["outlet"].get("name", ""),
                "address": context["outlet"].get("address", ""),
                "phone": context["outlet"].get("phone", ""),
            },
            "fulfilment": {
                "address": context["address"],
                "city": context["city"],
                "area": context["area"],
                "latitude": context["latitude"],
                "longitude": context["longitude"],
                "distance_km": context.get("delivery_distance_km"),
            },
            "delivery_pricing": {
                "source": context.get("delivery_pricing_source", ""),
                "tier_id": context.get("delivery_pricing_tier_id"),
                "tier_label": context.get("delivery_pricing_tier_label", ""),
                "distance_km": context.get("delivery_distance_km"),
                "charge_scope": "order",
                "applies_once_per_order": context["service_mode"] == "delivery",
            },
            "items": validated_items,
            "subtotal_minor": subtotal_minor,
            "delivery_fee_minor": delivery_fee_minor,
            "total_minor": total_minor,
            "currency": "PKR",
            "payment_method": payment_method,
            "payment_status": initial_payment_status,
            "payment_id": payment_id,
            "order_status": "order_placed",
            "customer_notes": context["order_notes"],
            "internal_notes": [],
            "schedule_for": context["schedule_for"],
            "operational_hold": ("payment_pending" if payment_method == "easypaisa" else ("bank_verification_pending" if payment_method == "bank_transfer" else None)),
            "placed_at": firestore.SERVER_TIMESTAMP,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        payment_doc = {
            "payment_id": payment_id,
            "order_id": order_id,
            "customer_uid": p["uid"],
            "method": payment_method,
            "amount_minor": total_minor,
            "currency": "PKR",
            "status": initial_payment_status,
            "provider_transaction_id": None,
            "payer_phone": easypaisa_payer_phone if payment_method == "easypaisa" else "",
            "customer_reference": validate_text(data.get("bank_reference"), "bank_reference", max_len=120)
            if payment_method == "bank_transfer"
            else "",
            "initiation_state": "not_started" if payment_method == "easypaisa" else "not_applicable",
            "payment_request_status": "not_sent" if payment_method == "easypaisa" else "not_applicable",
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        history_doc = {
            "order_id": order_id,
            "previous_status": None,
            "new_status": "order_placed",
            "actor_uid": p["uid"],
            "actor_role": ROLE_CUSTOMER,
            "note": "Order submitted by customer.",
            "created_at": firestore.SERVER_TIMESTAMP,
        }
        txn.set(order_ref, order_doc)
        txn.set(payment_ref, payment_doc)
        txn.set(history_ref, history_doc)
        txn.set(
            idem_ref,
            {
                "uid": p["uid"],
                "key_hash": idem_id,
                "order_id": order_id,
                "payment_id": payment_id,
                "created_at": firestore.SERVER_TIMESTAMP,
            },
        )
        txn.set(
            cart_ref,
            {
                "items": [],
                "outlet_id": None,
                "service_mode": None,
                "revision": expected_revision + 1,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return {"duplicate": False, "order_id": order_id, "payment_id": payment_id}

    result = create_order_txn(transaction)
    actual_order_id = result["order_id"]
    actual_payment_id = result["payment_id"]
    order = get_document("orders", actual_order_id, required=True)
    payment = get_document("payments", actual_payment_id, required=True)

    if not result["duplicate"]:
        create_notification(
            recipient_uid=p["uid"],
            notification_type="transactional",
            title="Order received",
            message=f"Your order {order.get('order_reference')} has been placed.",
            related_order_id=actual_order_id,
        )
        if payment_method == "easypaisa":
            create_staff_notification(
                title="Advance payment pending",
                message=f"Order {order.get('order_reference')} is waiting for Easypaisa customer approval.",
                related_order_id=actual_order_id,
                outlet_id=context["outlet_id"],
            )
        else:
            create_staff_notification(
                title="New order",
                message=f"New {context['service_mode'].replace('_', ' ')} order {order.get('order_reference')} received.",
                related_order_id=actual_order_id,
                outlet_id=context["outlet_id"],
            )
        audit_log("order.created", "order", actual_order_id, {"payment_method": payment_method, "total_minor": total_minor})

    provider_data = None
    if payment_method == "easypaisa":
        if payment.get("initiation_state") != "initiated":
            provider_data = initiate_easypaisa_payment(actual_payment_id)
            order = get_document("orders", actual_order_id, required=True)
            payment = get_document("payments", actual_payment_id, required=True)
        else:
            stored_session = payment.get("provider_session")
            provider_data = stored_session if isinstance(stored_session, dict) else None

    response_data = {
        "order": order,
        "payment": payment,
        "provider": provider_data,
        "duplicate": bool(result["duplicate"]),
    }
    if request.path == "/checkout" and not request.is_json:
        if payment_method == "easypaisa" and isinstance(provider_data, dict):
            redirect_url = str(provider_data.get("redirect_url") or "").strip()
            if redirect_url:
                if redirect_url.startswith("/") and not redirect_url.startswith("//"):
                    payment_redirect = redirect_url
                else:
                    payment_redirect = validate_url(redirect_url, "payment redirect", required=True)
                flash("Order created. Complete Easypaisa payment to continue.", "info")
                return redirect(payment_redirect)
        flash("Order submitted successfully.", "success")
        return redirect(url_for("order_detail_page", order_id=actual_order_id))
    return api_success(response_data, 200 if result["duplicate"] else 201)


# =============================================================================
# Payments: Easypaisa, bank transfer, COD state
# =============================================================================

def customer_can_access_payment(payment: dict[str, Any], uid: str) -> bool:
    return payment.get("customer_uid") == uid


def initiate_easypaisa_payment(payment_id: str) -> dict[str, Any]:
    """Initiate either the demo wallet-request flow or the configured live adapter."""
    payment = get_document(
        "payments",
        validate_id(payment_id, "payment_id"),
        required=True,
        code="PAYMENT_NOT_FOUND",
    )
    if payment.get("method") != "easypaisa":
        raise AppError("INVALID_PAYMENT_METHOD", "This payment is not an Easypaisa payment.", 409)
    if payment.get("status") == "paid":
        return {"already_paid": True}
    if payment.get("status") != "pending":
        raise AppError(
            "INVALID_PAYMENT_TRANSITION",
            "This Easypaisa payment cannot be initiated from its current state.",
            409,
        )

    if payment.get("initiation_state") == "initiated" and payment.get("provider_session"):
        session_data = payment.get("provider_session")
        return session_data if isinstance(session_data, dict) else {}

    order = get_document(
        "orders",
        payment.get("order_id", ""),
        required=True,
        code="ORDER_NOT_FOUND",
    )
    if order.get("customer_uid") != payment.get("customer_uid"):
        raise AppError("PAYMENT_ORDER_MISMATCH", "Payment and order ownership do not match.", 409)
    if str(order.get("payment_id") or "") != payment["id"]:
        raise AppError("PAYMENT_ORDER_MISMATCH", "Payment and order linkage do not match.", 409)
    if order.get("payment_method") != "easypaisa":
        raise AppError("PAYMENT_ORDER_MISMATCH", "The linked order is not an Easypaisa order.", 409)
    if order.get("order_status") == "cancelled":
        raise AppError("ORDER_CANCELLED", "A cancelled order cannot start payment.", 409)

    payer_phone = normalize_easypaisa_mobile(
        payment.get("payer_phone")
        or (order.get("customer_snapshot") or {}).get("phone"),
        required=True,
    )

    try:
        amount_minor = int(payment.get("amount_minor"))
        order_total_minor = int(order.get("total_minor"))
    except (TypeError, ValueError):
        raise ConfigurationError("PAYMENT_AMOUNT_INVALID", "Stored payment or order amount is invalid.")

    if amount_minor <= 0 or amount_minor != order_total_minor:
        raise AppError(
            "PAYMENT_AMOUNT_MISMATCH",
            "Stored payment amount does not match the linked order total.",
            409,
        )

    currency = str(payment.get("currency") or "").strip().upper()
    order_currency = str(order.get("currency") or "").strip().upper()
    if not currency or currency != order_currency:
        raise AppError(
            "PAYMENT_CURRENCY_MISMATCH",
            "Stored payment currency does not match the linked order.",
            409,
        )

    if demo_easypaisa_enabled():
        provider_request_id = f"EPD_{secrets.token_urlsafe(12)}"
        public_session = {
            "provider": "easypaisa",
            "mode": "demo",
            "flow": "wallet_request",
            "status": "pending_customer_approval",
            "redirect_url": url_for(
                "demo_easypaisa_page",
                payment_id=payment["id"],
            ),
            "request_id": provider_request_id,
            "payer_phone_masked": mask_mobile_number(payer_phone),
            "amount": money_fields(amount_minor),
        }
        require_db().collection("payments").document(payment["id"]).set(
            {
                "payer_phone": payer_phone,
                "initiation_state": "initiated",
                "payment_request_status": "sent",
                "provider_mode": "demo",
                "provider_request_id": provider_request_id,
                "provider_session": public_session,
                "requested_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return public_session

    result = easypaisa_call(
        "initiate",
        {
            "payment_id": payment["id"],
            "order_id": order["id"],
            "order_reference": order.get("order_reference"),
            "amount_minor": amount_minor,
            "currency": currency,
            "payer_phone": payer_phone,
            "callback_url": f"{BASE_URL}/api/payments/easypaisa/callback",
            "return_url": f"{BASE_URL}/order/{order['id']}",
        },
    )
    if not result.get("accepted", False):
        require_db().collection("payments").document(payment["id"]).set(
            {
                "status": "failed",
                "initiation_state": "failed",
                "payment_request_status": "failed",
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        require_db().collection("orders").document(order["id"]).set(
            {
                "payment_status": "failed",
                "operational_hold": "payment_failed",
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        raise AppError(
            "PAYMENT_INITIATION_FAILED",
            "Easypaisa payment could not be initiated.",
            502,
        )

    public_session = result.get("public_session") or {}
    if not isinstance(public_session, dict):
        public_session = {}

    redirect_url = str(public_session.get("redirect_url") or "").strip()
    if redirect_url:
        validate_url(redirect_url, "payment redirect", required=True)

    provider_transaction_id = validate_text(
        result.get("provider_transaction_id"),
        "provider_transaction_id",
        max_len=200,
    )
    require_db().collection("payments").document(payment["id"]).set(
        {
            "payer_phone": payer_phone,
            "initiation_state": "initiated",
            "payment_request_status": "sent",
            "provider_transaction_id": provider_transaction_id or None,
            "provider_session": public_session,
            "requested_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    return public_session


@app.post("/api/payments/<payment_id>/easypaisa/initiate")
@limiter.limit("10 per 15 minutes")
@customer_required
def easypaisa_initiate_api(payment_id: str):
    p = current_principal()
    payment = get_document("payments", validate_id(payment_id, "payment_id"), required=True, code="PAYMENT_NOT_FOUND")
    if not customer_can_access_payment(payment, p["uid"]):
        raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)
    return api_success(initiate_easypaisa_payment(payment["id"]))



@app.get("/payments/easypaisa/demo/<payment_id>")
@customer_required
def demo_easypaisa_page(payment_id: str):
    if not demo_easypaisa_enabled():
        raise AppError("DEMO_PAYMENT_UNAVAILABLE", "Demo payment gateway is unavailable.", 404)

    p = current_principal()
    payment = get_document(
        "payments",
        validate_id(payment_id, "payment_id"),
        required=True,
        code="PAYMENT_NOT_FOUND",
    )
    if not customer_can_access_payment(payment, p["uid"]):
        raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)
    if payment.get("method") != "easypaisa":
        raise AppError("INVALID_PAYMENT_METHOD", "This payment is not an Easypaisa payment.", 409)

    order = get_document(
        "orders",
        payment.get("order_id", ""),
        required=True,
        code="ORDER_NOT_FOUND",
    )
    if order.get("customer_uid") != p["uid"]:
        raise AppError("ORDER_NOT_FOUND", "Order was not found.", 404)

    if payment.get("initiation_state") != "initiated":
        initiate_easypaisa_payment(payment["id"])
        payment = get_document("payments", payment["id"], required=True, code="PAYMENT_NOT_FOUND")

    return render_page(
        "demo_easypaisa.html",
        payment=payment,
        order=customer_safe_order(order),
        payer_phone_masked=mask_mobile_number(payment.get("payer_phone")),
        provider_request_id=str(payment.get("provider_request_id") or ""),
        payment_request_status=str(payment.get("payment_request_status") or "sent"),
        confirm_url=url_for("demo_easypaisa_confirm", payment_id=payment["id"]),
        cancel_url=url_for("demo_easypaisa_cancel", payment_id=payment["id"]),
        order_url=url_for("order_detail_page", order_id=order["id"]),
        demo_number="",
        demo_account_title="",
    )


def confirm_demo_easypaisa_payment(payment_id: str, uid: str) -> dict[str, Any]:
    """Simulate customer approval of an Easypaisa wallet request in development."""
    if not demo_easypaisa_enabled():
        raise AppError("DEMO_PAYMENT_UNAVAILABLE", "Demo payment gateway is unavailable.", 404)

    db = require_db()
    payment_id = validate_id(payment_id, "payment_id")
    uid = validate_id(uid, "uid")
    payment_ref = db.collection("payments").document(payment_id)
    transaction = db.transaction()

    @firestore.transactional
    def confirm_txn(txn):
        payment_snap = payment_ref.get(transaction=txn)
        if not payment_snap.exists:
            raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)

        payment = payment_snap.to_dict() or {}
        if payment.get("customer_uid") != uid:
            raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)
        if payment.get("method") != "easypaisa":
            raise AppError("INVALID_PAYMENT_METHOD", "This payment is not an Easypaisa payment.", 409)

        order_id = validate_id(payment.get("order_id"), "order_id")
        if payment.get("status") == "paid":
            return {
                "duplicate": True,
                "order_id": order_id,
                "payment_id": payment_id,
                "payment_status": "paid",
            }

        if payment.get("status") != "pending":
            raise AppError(
                "INVALID_PAYMENT_TRANSITION",
                "This demo payment can no longer be approved from its current state.",
                409,
            )
        if payment.get("initiation_state") != "initiated":
            raise AppError("PAYMENT_NOT_INITIATED", "The Easypaisa payment request has not been sent.", 409)
        if payment.get("payment_request_status") != "sent":
            raise AppError(
                "PAYMENT_REQUEST_NOT_PENDING",
                "The Easypaisa payment request is not waiting for customer approval.",
                409,
            )

        payer_phone = normalize_easypaisa_mobile(payment.get("payer_phone"), required=True)
        provider_request_id = validate_id(payment.get("provider_request_id"), "provider_request_id")

        order_ref = db.collection("orders").document(order_id)
        order_snap = order_ref.get(transaction=txn)
        if not order_snap.exists:
            raise AppError("ORDER_NOT_FOUND", "Linked order was not found.", 404)

        order = order_snap.to_dict() or {}
        if order.get("customer_uid") != uid:
            raise AppError("PAYMENT_ORDER_MISMATCH", "Payment and order ownership do not match.", 409)
        if str(order.get("payment_id") or "") != payment_id:
            raise AppError("PAYMENT_ORDER_MISMATCH", "Payment and order linkage do not match.", 409)
        if order.get("payment_method") != "easypaisa":
            raise AppError("PAYMENT_ORDER_MISMATCH", "The linked order is not an Easypaisa order.", 409)
        if order.get("order_status") == "cancelled":
            raise AppError("ORDER_CANCELLED", "A cancelled order cannot be paid.", 409)

        try:
            payment_amount_minor = int(payment.get("amount_minor"))
            order_total_minor = int(order.get("total_minor"))
        except (TypeError, ValueError):
            raise ConfigurationError("PAYMENT_AMOUNT_INVALID", "Stored payment or order amount is invalid.")

        if payment_amount_minor <= 0 or payment_amount_minor != order_total_minor:
            raise AppError(
                "PAYMENT_AMOUNT_MISMATCH",
                "Stored payment amount does not match the linked order total.",
                409,
            )

        payment_currency = str(payment.get("currency") or "").strip().upper()
        order_currency = str(order.get("currency") or "").strip().upper()
        if not payment_currency or payment_currency != order_currency:
            raise AppError(
                "PAYMENT_CURRENCY_MISMATCH",
                "Stored payment currency does not match the linked order.",
                409,
            )

        provider_transaction_id = f"DEMO-{secrets.token_hex(8).upper()}"
        event_id = hashlib.sha256(
            f"easypaisa_demo:{provider_request_id}:approved".encode()
        ).hexdigest()
        event_ref = db.collection("payment_events").document(event_id)

        provider_session = payment.get("provider_session")
        if not isinstance(provider_session, dict):
            provider_session = {}
        provider_session = {
            **provider_session,
            "status": "approved",
            "payer_phone_masked": mask_mobile_number(payer_phone),
        }

        txn.set(
            event_ref,
            {
                "provider": "easypaisa_demo",
                "provider_event_id": f"{provider_request_id}:approved",
                "provider_request_id": provider_request_id,
                "provider_transaction_id": provider_transaction_id,
                "payment_id": payment_id,
                "order_id": order_id,
                "verified": True,
                "status": "paid",
                "demo": True,
                "amount_minor": payment_amount_minor,
                "currency": payment_currency,
                "created_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        txn.update(
            payment_ref,
            {
                "status": "paid",
                "initiation_state": "completed",
                "payment_request_status": "approved",
                "provider_transaction_id": provider_transaction_id,
                "provider_mode": "demo",
                "provider_session": provider_session,
                "paid_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )
        txn.update(
            order_ref,
            {
                "payment_status": "paid",
                "operational_hold": None,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )

        return {
            "duplicate": False,
            "order_id": order_id,
            "payment_id": payment_id,
            "payment_status": "paid",
        }

    return confirm_txn(transaction)


@app.post("/payments/easypaisa/demo/<payment_id>/confirm")
@limiter.limit("20 per hour")
@customer_required
def demo_easypaisa_confirm(payment_id: str):
    p = current_principal()
    result = confirm_demo_easypaisa_payment(payment_id, p["uid"])

    if not result["duplicate"]:
        audit_log(
            "payment.easypaisa_demo_paid",
            "payment",
            result["payment_id"],
            {"demo": True, "order_id": result["order_id"]},
            actor=p,
        )
        order = get_document("orders", result["order_id"])
        if order:
            create_notification(
                recipient_uid=p["uid"],
                notification_type="transactional",
                title="Payment approved",
                message=f"Easypaisa demo payment for order {order.get('order_reference')} was approved.",
                related_order_id=order["id"],
            )
            create_staff_notification(
                title="Advance payment received",
                message=f"Order {order.get('order_reference')} is paid and ready for processing.",
                related_order_id=order["id"],
                outlet_id=str(order.get("outlet_id") or ""),
            )

    flash("Demo Easypaisa payment approved.", "success")
    return redirect(url_for("order_detail_page", order_id=result["order_id"]))


def cancel_demo_easypaisa_payment(payment_id: str, uid: str) -> dict[str, Any]:
    """Simulate customer decline/cancellation of an Easypaisa wallet request."""
    if not demo_easypaisa_enabled():
        raise AppError("DEMO_PAYMENT_UNAVAILABLE", "Demo payment gateway is unavailable.", 404)

    db = require_db()
    payment_id = validate_id(payment_id, "payment_id")
    uid = validate_id(uid, "uid")
    payment_ref = db.collection("payments").document(payment_id)
    history_ref = db.collection("order_status_history").document()
    transaction = db.transaction()

    @firestore.transactional
    def cancel_txn(txn):
        payment_snap = payment_ref.get(transaction=txn)
        if not payment_snap.exists:
            raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)

        payment = payment_snap.to_dict() or {}
        if payment.get("customer_uid") != uid:
            raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)
        if payment.get("method") != "easypaisa":
            raise AppError("INVALID_PAYMENT_METHOD", "This payment is not an Easypaisa payment.", 409)
        if payment.get("status") == "paid":
            raise AppError("PAYMENT_ALREADY_PAID", "A completed payment cannot be cancelled.", 409)

        order_id = validate_id(payment.get("order_id"), "order_id")
        order_ref = db.collection("orders").document(order_id)
        order_snap = order_ref.get(transaction=txn)
        if not order_snap.exists:
            raise AppError("ORDER_NOT_FOUND", "Linked order was not found.", 404)

        order = order_snap.to_dict() or {}
        if order.get("customer_uid") != uid or str(order.get("payment_id") or "") != payment_id:
            raise AppError("PAYMENT_ORDER_MISMATCH", "Payment and order linkage do not match.", 409)

        if payment.get("status") == "cancelled" and order.get("order_status") == "cancelled":
            return {
                "duplicate": True,
                "order_id": order_id,
                "payment_id": payment_id,
                "payment_status": "cancelled",
            }

        if payment.get("status") != "pending":
            raise AppError(
                "INVALID_PAYMENT_TRANSITION",
                "This Easypaisa payment can no longer be declined from its current state.",
                409,
            )
        if payment.get("initiation_state") != "initiated":
            raise AppError("PAYMENT_NOT_INITIATED", "The Easypaisa payment request has not been sent.", 409)

        provider_request_id = validate_id(payment.get("provider_request_id"), "provider_request_id")
        current_order_status = str(order.get("order_status") or "")
        if current_order_status != "order_placed":
            raise AppError(
                "ORDER_ALREADY_PROCESSING",
                "This order has already moved beyond the unpaid checkout stage.",
                409,
            )

        event_id = hashlib.sha256(
            f"easypaisa_demo:{provider_request_id}:declined".encode()
        ).hexdigest()
        event_ref = db.collection("payment_events").document(event_id)

        provider_session = payment.get("provider_session")
        if not isinstance(provider_session, dict):
            provider_session = {}
        provider_session = {**provider_session, "status": "declined"}

        txn.set(
            event_ref,
            {
                "provider": "easypaisa_demo",
                "provider_event_id": f"{provider_request_id}:declined",
                "provider_request_id": provider_request_id,
                "payment_id": payment_id,
                "order_id": order_id,
                "verified": True,
                "status": "cancelled",
                "demo": True,
                "created_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        txn.update(
            payment_ref,
            {
                "status": "cancelled",
                "initiation_state": "completed",
                "payment_request_status": "declined",
                "provider_mode": "demo",
                "provider_session": provider_session,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )
        txn.update(
            order_ref,
            {
                "payment_status": "cancelled",
                "operational_hold": "payment_cancelled",
                "order_status": "cancelled",
                "cancellation_reason": "Customer declined the demo Easypaisa payment request.",
                "cancelled_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )
        txn.set(
            history_ref,
            {
                "order_id": order_id,
                "previous_status": current_order_status,
                "new_status": "cancelled",
                "actor_uid": uid,
                "actor_role": ROLE_CUSTOMER,
                "note": "Customer declined the demo Easypaisa payment request.",
                "created_at": firestore.SERVER_TIMESTAMP,
            },
        )
        return {
            "duplicate": False,
            "order_id": order_id,
            "payment_id": payment_id,
            "payment_status": "cancelled",
        }

    return cancel_txn(transaction)


@app.post("/payments/easypaisa/demo/<payment_id>/cancel")
@limiter.limit("20 per hour")
@customer_required
def demo_easypaisa_cancel(payment_id: str):
    p = current_principal()
    result = cancel_demo_easypaisa_payment(payment_id, p["uid"])

    if not result["duplicate"]:
        audit_log(
            "payment.easypaisa_demo_declined",
            "payment",
            result["payment_id"],
            {"demo": True, "order_id": result["order_id"]},
            actor=p,
        )

    flash("Demo Easypaisa payment was cancelled.", "warning")
    return redirect(url_for("order_detail_page", order_id=result["order_id"]))


@app.post("/api/payments/easypaisa/callback")
@csrf.exempt
@limiter.limit("120 per minute")
def easypaisa_callback_api():
    raw_body = request.get_data(cache=True)
    headers = {k: v for k, v in request.headers.items()}
    result = easypaisa_call(
        "verify_callback",
        {
            "raw_body": raw_body,
            "json": request.get_json(silent=True),
            "form": request.form.to_dict(flat=True),
            "headers": headers,
        },
    )
    if not result.get("verified", False):
        logger.warning("Rejected unverified Easypaisa callback.")
        raise AppError("PAYMENT_CALLBACK_INVALID", "Payment callback verification failed.", 401)

    payment_id = validate_id(result.get("payment_id"), "payment_id")
    provider_event_id = validate_text(result.get("provider_event_id"), "provider_event_id", required=True, max_len=200)
    provider_transaction_id = validate_text(result.get("provider_transaction_id"), "provider_transaction_id", max_len=200)
    provider_status = str(result.get("status") or "").strip().lower()
    status_map = {
        "paid": "paid",
        "success": "paid",
        "successful": "paid",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "pending": "pending",
    }
    new_status = status_map.get(provider_status)
    if not new_status:
        raise AppError("PAYMENT_CALLBACK_INVALID", "Unsupported verified provider payment state.", 400)

    db = require_db()
    payment_ref = db.collection("payments").document(payment_id)
    event_ref = db.collection("payment_events").document(
        hashlib.sha256(f"easypaisa:{provider_event_id}".encode()).hexdigest()
    )
    transaction = db.transaction()

    @firestore.transactional
    def payment_callback_txn(txn):
        event_snap = event_ref.get(transaction=txn)
        payment_snap = payment_ref.get(transaction=txn)
        if event_snap.exists:
            return {"duplicate": True, "order_id": (event_snap.to_dict() or {}).get("order_id")}
        if not payment_snap.exists:
            raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)
        payment = payment_snap.to_dict() or {}
        if payment.get("method") != "easypaisa":
            raise AppError("PAYMENT_CALLBACK_INVALID", "Callback does not belong to an Easypaisa payment.", 400)
        if new_status == "paid":
            callback_amount = result.get("amount_minor")
            callback_currency = str(result.get("currency") or "").strip().upper()
            try:
                callback_amount_minor = int(callback_amount)
            except (TypeError, ValueError):
                raise AppError(
                    "PAYMENT_CALLBACK_INVALID",
                    "Verified paid callbacks must include the provider-confirmed amount_minor.",
                    400,
                )
            if callback_amount_minor != int(payment.get("amount_minor", -1)):
                raise AppError("PAYMENT_AMOUNT_MISMATCH", "Payment callback amount does not match the order amount.", 400)
            if callback_currency != str(payment.get("currency") or "PKR").upper():
                raise AppError("PAYMENT_CURRENCY_MISMATCH", "Payment callback currency does not match the payment record.", 400)
            if not provider_transaction_id:
                raise AppError("PAYMENT_CALLBACK_INVALID", "Paid callback is missing the provider transaction identifier.", 400)
        order_id = payment.get("order_id")
        order_ref = db.collection("orders").document(order_id)
        order_snap = order_ref.get(transaction=txn)
        if not order_snap.exists:
            raise AppError("ORDER_NOT_FOUND", "Linked order was not found.", 404)

        order_data = order_snap.to_dict() or {}
        if str(order_data.get("payment_id") or "") != payment_id:
            raise AppError("PAYMENT_ORDER_MISMATCH", "Payment and order linkage do not match.", 409)
        if order_data.get("payment_method") != "easypaisa":
            raise AppError("PAYMENT_ORDER_MISMATCH", "The linked order is not an Easypaisa order.", 409)
        if order_data.get("customer_uid") != payment.get("customer_uid"):
            raise AppError("PAYMENT_ORDER_MISMATCH", "Payment and order ownership do not match.", 409)

        previous_status = payment.get("status")
        # Do not downgrade a confirmed paid payment because of a late/duplicate provider event.
        effective_status = "paid" if previous_status == "paid" else new_status
        payment_request_status = {
            "paid": "approved",
            "failed": "failed",
            "cancelled": "declined",
            "pending": "sent",
        }.get(effective_status, "sent")
        initiation_state = "completed" if effective_status in {"paid", "failed", "cancelled"} else "initiated"
        txn.set(
            event_ref,
            {
                "provider": "easypaisa",
                "provider_event_id": provider_event_id,
                "provider_transaction_id": provider_transaction_id or payment.get("provider_transaction_id"),
                "payment_id": payment_id,
                "order_id": order_id,
                "verified": True,
                "status": new_status,
                "created_at": firestore.SERVER_TIMESTAMP,
            },
        )
        txn.update(
            payment_ref,
            {
                "status": effective_status,
                "initiation_state": initiation_state,
                "payment_request_status": payment_request_status,
                "provider_transaction_id": provider_transaction_id or payment.get("provider_transaction_id"),
                "updated_at": firestore.SERVER_TIMESTAMP,
                "paid_at": firestore.SERVER_TIMESTAMP if effective_status == "paid" and previous_status != "paid" else payment.get("paid_at"),
            },
        )
        txn.update(
            order_ref,
            {
                "payment_status": effective_status,
                "operational_hold": (
                    None
                    if effective_status == "paid"
                    else (
                        "payment_failed"
                        if effective_status == "failed"
                        else ("payment_cancelled" if effective_status == "cancelled" else "payment_pending")
                    )
                ),
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )
        return {"duplicate": False, "order_id": order_id, "effective_status": effective_status}

    outcome = payment_callback_txn(transaction)
    if not outcome["duplicate"]:
        audit_log(
            "payment.easypaisa_callback_verified",
            "payment",
            payment_id,
            {"status": outcome.get("effective_status"), "provider_event_id_hash": hashlib.sha256(provider_event_id.encode()).hexdigest()},
            actor={"uid": "easypaisa_provider", "role": "system"},
        )
        if outcome.get("effective_status") == "paid":
            order = get_document("orders", outcome["order_id"])
            if order:
                create_notification(
                    recipient_uid=order.get("customer_uid"),
                    notification_type="transactional",
                    title="Payment received",
                    message=f"Payment for order {order.get('order_reference')} was confirmed.",
                    related_order_id=order["id"],
                )
                create_staff_notification(
                    title="Advance payment received",
                    message=f"Order {order.get('order_reference')} is paid and ready for processing.",
                    related_order_id=order["id"],
                    outlet_id=str(order.get("outlet_id") or ""),
                )
    return api_success({"received": True, "duplicate": outcome["duplicate"]})


@app.post("/api/payments/<payment_id>/bank-reference")
@limiter.limit("20 per hour")
@customer_required
def submit_bank_reference(payment_id: str):
    p = current_principal()
    payment = get_document("payments", validate_id(payment_id, "payment_id"), required=True, code="PAYMENT_NOT_FOUND")
    if payment.get("customer_uid") != p["uid"]:
        raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)
    if payment.get("method") != "bank_transfer":
        raise AppError("INVALID_PAYMENT_METHOD", "This payment is not a bank transfer.", 409)
    if payment.get("status") == "paid":
        raise AppError("PAYMENT_ALREADY_PAID", "Payment is already confirmed.", 409)
    data = payload_data()
    reference = validate_text(data.get("transaction_reference"), "transaction_reference", required=True, min_len=3, max_len=120)
    require_db().collection("payments").document(payment["id"]).set(
        {
            "customer_reference": reference,
            "status": "awaiting_verification",
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    audit_log("payment.bank_reference_submitted", "payment", payment["id"], {"reference_length": len(reference)})
    return api_success(get_document("payments", payment["id"], required=True))


# =============================================================================
# Notifications
# =============================================================================

def create_notification(
    *,
    recipient_uid: str,
    notification_type: str,
    title: str,
    message: str,
    related_order_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    if not recipient_uid:
        return None
    if notification_type not in {"transactional", "marketing", "system"}:
        logger.warning("Ignored notification with unsupported type=%s", notification_type)
        return None
    try:
        db = require_db()
        if notification_type == "marketing":
            user_snap = db.collection("users").document(recipient_uid).get()
            user = user_snap.to_dict() if user_snap.exists else {}
            if not bool((user or {}).get("marketing_notifications_enabled", False)):
                return None
        ref = db.collection("notifications").document()
        ref.set(
            {
                "recipient_uid": recipient_uid,
                "notification_type": notification_type,
                "title": validate_text(title, "title", required=True, max_len=120),
                "message": validate_text(message, "message", required=True, max_len=500),
                "related_order_id": related_order_id,
                "metadata": metadata or {},
                "read": False,
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return ref.id
    except Exception:
        logger.exception("Non-fatal notification creation failure.")
        return None


def create_staff_notification(*, title: str, message: str, related_order_id: str, outlet_id: str) -> Optional[str]:
    try:
        db = require_db()
        ref = db.collection("notifications").document()
        ref.set(
            {
                "audience": "staff",
                "required_permission": "view_orders",
                "outlet_id": outlet_id,
                "notification_type": "transactional",
                "title": title,
                "message": message,
                "related_order_id": related_order_id,
                "read_by": [],
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return ref.id
    except Exception:
        logger.exception("Non-fatal staff notification creation failure.")
        return None


@app.get("/api/notifications")
@login_required
def notifications_api():
    p = current_principal()
    q = firestore_query("notifications", [("recipient_uid", "==", p["uid"])])
    items, cursor = paginate_query(
        "notifications",
        q,
        order_field="created_at",
        limit=query_limit(),
        cursor=request.args.get("cursor"),
    )
    return api_success({"items": items, "next_cursor": cursor})


@app.post("/api/notifications/<notification_id>/read")
@login_required
def mark_notification_read(notification_id: str):
    p = current_principal()
    notification = get_document("notifications", validate_id(notification_id, "notification_id"), required=True, code="NOTIFICATION_NOT_FOUND")
    if notification.get("recipient_uid") != p["uid"]:
        raise AppError("NOTIFICATION_NOT_FOUND", "Notification was not found.", 404)
    require_db().collection("notifications").document(notification["id"]).set(
        {"read": True, "read_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    return api_success({"id": notification["id"], "read": True})


# =============================================================================
# Customer orders and tracking
# =============================================================================

def customer_order(order_id: str, uid: str) -> dict[str, Any]:
    order = get_document("orders", validate_id(order_id, "order_id"), required=True, code="ORDER_NOT_FOUND")
    if order.get("customer_uid") != uid:
        # Deliberately use 404 to avoid disclosing existence of another customer's order.
        raise AppError("ORDER_NOT_FOUND", "Order was not found.", 404)
    return order


def order_history(order_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return ordered_documents(
        "order_status_history",
        [("order_id", "==", order_id)],
        order_field="created_at",
        direction="asc",
        limit=limit,
    )


def customer_safe_order(order: dict[str, Any]) -> dict[str, Any]:
    safe = dict(order)
    safe.pop("internal_notes", None)
    return safe


def customer_order_history_page(
    uid: str,
    *,
    limit: int,
    cursor: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Return one customer's orders newest-first with a safe index fallback.

    The preferred Firestore query keeps the efficient customer_uid + placed_at
    ordering when the composite index exists. On a development database where
    that index has not been deployed yet, fall back to the known customer_uid
    equality query and sort the bounded result in Python.

    This targeted fallback intentionally does not inspect Firestore client's
    private query-filter internals, so a matching customer order cannot silently
    disappear merely because those internals changed.
    """
    db = require_db()
    uid = validate_id(uid, "uid")
    page_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    cursor_id = validate_id(cursor, "cursor") if cursor else None

    base_query = firestore_query("orders", [("customer_uid", "==", uid)])
    ordered_query = base_query.order_by("placed_at", direction=firestore.Query.DESCENDING)

    if cursor_id:
        cursor_snap = db.collection("orders").document(cursor_id).get()
        if not cursor_snap.exists:
            raise AppError("INVALID_CURSOR", "Order history pagination cursor is invalid.", 400)
        cursor_data = cursor_snap.to_dict() or {}
        if str(cursor_data.get("customer_uid") or "") != uid:
            raise AppError("INVALID_CURSOR", "Order history pagination cursor is invalid.", 400)
        ordered_query = ordered_query.start_after(cursor_snap)

    try:
        docs = list(ordered_query.limit(page_limit + 1).stream())
        has_more = len(docs) > page_limit
        docs = docs[:page_limit]
        page = [item for item in (doc_dict(doc) for doc in docs) if item]
        next_cursor = docs[-1].id if has_more and docs else None
        return page, next_cursor
    except FailedPrecondition as exc:
        if not (_is_missing_firestore_index_error(exc) and ALLOW_FIRESTORE_INDEX_FALLBACK):
            raise

        logger.warning(
            "Missing Firestore composite index for customer orders ordered by placed_at; "
            "using explicit customer_uid development fallback."
        )

        # A single-field equality query is covered by Firestore's normal
        # single-field index. Keep the fallback bounded for development safety.
        docs = list(base_query.limit(FIRESTORE_FALLBACK_SCAN_LIMIT).stream())
        items = [item for item in (doc_dict(doc) for doc in docs) if item]
        items.sort(
            key=lambda item: _python_sort_key(_nested_value(item, "placed_at")),
            reverse=True,
        )

        start = 0
        if cursor_id:
            positions = [idx for idx, item in enumerate(items) if item.get("id") == cursor_id]
            if not positions:
                raise AppError(
                    "INVALID_CURSOR",
                    "Order history pagination cursor is outside the fallback scan window.",
                    400,
                )
            start = positions[0] + 1

        page = items[start:start + page_limit + 1]
        has_more = len(page) > page_limit
        page = page[:page_limit]
        next_cursor = page[-1]["id"] if has_more and page else None
        return page, next_cursor


@app.get("/orders")
@customer_required
def orders_page():
    p = current_principal()
    orders, cursor = customer_order_history_page(
        p["uid"],
        limit=DEFAULT_PAGE_SIZE,
        cursor=request.args.get("cursor"),
    )
    return render_page("orders.html", orders=[customer_safe_order(x) for x in orders], next_cursor=cursor)


@app.get("/api/orders")
@customer_required
def customer_orders_api():
    p = current_principal()
    orders, cursor = customer_order_history_page(
        p["uid"],
        limit=query_limit(),
        cursor=request.args.get("cursor"),
    )
    return api_success({"orders": [customer_safe_order(x) for x in orders], "next_cursor": cursor})


@app.get("/order/<order_id>")
@customer_required
def order_detail_page(order_id: str):
    p = current_principal()
    order = customer_order(order_id, p["uid"])
    history = order_history(order["id"])
    payment = get_document("payments", order.get("payment_id", "")) if order.get("payment_id") else None
    return render_page("order.html", order=customer_safe_order(order), history=history, payment=payment)


@app.get("/api/orders/<order_id>")
@customer_required
def order_detail_api(order_id: str):
    p = current_principal()
    order = customer_order(order_id, p["uid"])
    return api_success(
        {
            "order": customer_safe_order(order),
            "history": order_history(order["id"]),
            "payment": get_document("payments", order.get("payment_id", "")) if order.get("payment_id") else None,
        }
    )


@app.get("/track-order/<order_id>")
@customer_required
def track_order_page(order_id: str):
    p = current_principal()
    order = customer_order(order_id, p["uid"])
    return render_page("track_order.html", order=customer_safe_order(order), timeline=order_history(order["id"]))


@app.get("/api/track-order/<order_id>")
@customer_required
def track_order_api(order_id: str):
    p = current_principal()
    order = customer_order(order_id, p["uid"])
    return api_success(
        {
            "order_id": order["id"],
            "order_reference": order.get("order_reference"),
            "current_status": order.get("order_status"),
            "payment_status": order.get("payment_status"),
            "service_mode": order.get("service_mode"),
            "outlet": order.get("outlet_snapshot"),
            "fulfilment": order.get("fulfilment"),
            "items": order.get("items", []),
            "placed_at": order.get("placed_at"),
            "schedule_for": order.get("schedule_for"),
            "timeline": order_history(order["id"]),
        }
    )


# =============================================================================
# Reviews
# =============================================================================

def validate_review_eligibility(uid: str, product_id: str, order_id: str) -> dict[str, Any]:
    order = customer_order(order_id, uid)
    terminal = TERMINAL_SUCCESS_STATUS.get(order.get("service_mode"))
    if not terminal or order.get("order_status") != terminal:
        raise AppError("REVIEW_NOT_ELIGIBLE", "You can review products only after the order is completed.", 403)
    found = any(
        item.get("item_type") == "product" and item.get("product_id") == product_id
        for item in order.get("items", [])
    )
    if not found:
        # Deals may contain product IDs in their snapshot.
        for item in order.get("items", []):
            if item.get("item_type") != "deal":
                continue
            for included in item.get("included_items", []) or []:
                if isinstance(included, dict) and included.get("product_id") == product_id:
                    found = True
                    break
    if not found:
        raise AppError("REVIEW_NOT_ELIGIBLE", "This product was not part of the selected order.", 403)
    return order


@app.post("/api/reviews")
@limiter.limit("10 per hour")
@customer_required
def create_review_api():
    p = current_principal()
    data = payload_data()
    product_id = validate_id(data.get("product_id"), "product_id")
    order_id = validate_id(data.get("order_id"), "order_id")
    rating = validate_int(data.get("rating"), "rating", 1, 5)
    comment = validate_text(data.get("comment"), "comment", required=True, min_len=2, max_len=1500)
    get_product(product_id)
    validate_review_eligibility(p["uid"], product_id, order_id)

    duplicate = first_document(
        "reviews",
        [("customer_uid", "==", p["uid"]), ("product_id", "==", product_id), ("order_id", "==", order_id)],
    )
    if duplicate:
        raise AppError("DUPLICATE_REVIEW", "You already reviewed this product for this order.", 409)

    ref = require_db().collection("reviews").document()
    ref.set(
        {
            "customer_uid": p["uid"],
            "product_id": product_id,
            "order_id": order_id,
            "rating": rating,
            "comment": comment,
            "verified_order": True,
            "moderation_state": "pending",
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return api_success(get_document("reviews", ref.id, required=True), 201)


@app.get("/api/products/<product_id>/reviews")
def product_reviews_api(product_id: str):
    product_id = validate_id(product_id, "product_id")
    get_product(product_id)
    q = firestore_query("reviews", [("product_id", "==", product_id), ("moderation_state", "==", "approved")])
    items, cursor = paginate_query(
        "reviews", q, order_field="created_at", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"reviews": items, "next_cursor": cursor, "summary": public_review_summary(product_id)})


# =============================================================================
# Feedback, contact/support, and inquiries
# =============================================================================

def write_inquiry(kind: str, data: dict[str, Any]) -> str:
    name = validate_name(data.get("name"), "name", True)
    email = validate_email(data.get("email"), required=False)
    phone = validate_phone(data.get("phone"), required=False)
    if not email and not phone:
        raise AppError("VALIDATION_ERROR", "Provide an email address or phone number.", 422)
    subject = validate_text(data.get("subject") or data.get("category") or kind, "subject", required=True, max_len=120)
    message = validate_text(data.get("message"), "message", required=True, min_len=5, max_len=3000)
    ref = require_db().collection("inquiries").document()
    ref.set(
        {
            "kind": kind,
            "name": name,
            "email": email,
            "phone": phone,
            "subject": subject,
            "message": message,
            "status": "new",
            "customer_uid": (getattr(g, "principal", None) or {}).get("uid"),
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return ref.id


@app.route("/contact", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def contact_page():
    if request.method == "GET":
        return render_page("contact.html")
    inquiry_id = write_inquiry("contact", payload_data())
    if wants_json_response():
        return api_success({"inquiry_id": inquiry_id, "message": "Your message has been received."}, 201)
    flash("Your message has been received.", "success")
    return redirect(url_for("contact_page"))


@app.post("/api/support")
@limiter.limit("10 per hour")
def support_submit_api():
    inquiry_id = write_inquiry("support", payload_data())
    return api_success({"inquiry_id": inquiry_id, "message": "Support request received."}, 201)


@app.route("/feedback", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def feedback_page():
    if request.method == "GET":
        return render_page("feedback.html")
    data = payload_data()
    rating_raw = data.get("rating")
    rating = validate_int(rating_raw, "rating", 1, 5) if rating_raw is not None and rating_raw != "" else None
    comment = validate_text(data.get("comment"), "comment", required=False, max_len=2000)
    if rating is None and not comment:
        raise AppError("VALIDATION_ERROR", "Provide a rating or an optional experience comment.", 422)
    related_order = str(data.get("related_order_id") or "").strip()
    uid = (getattr(g, "principal", None) or {}).get("uid")
    if related_order:
        related_order = validate_id(related_order, "related_order_id")
        if uid:
            customer_order(related_order, uid)
        else:
            raise AppError("AUTH_REQUIRED", "Sign in to attach feedback to an order.", 401)
    ref = require_db().collection("feedback").document()
    ref.set(
        {
            "customer_uid": uid,
            "rating": rating,
            "comment": comment,
            "related_order_id": related_order or None,
            "status": "new",
            "moderation_state": "pending",
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    if wants_json_response():
        return api_success({"feedback_id": ref.id, "message": "Thank you for your feedback."}, 201)
    flash("Thank you for your feedback.", "success")
    return redirect(url_for("feedback_page"))


@app.post("/api/inquiries/birthday-parties")
@limiter.limit("5 per hour")
def birthday_inquiry_api():
    inquiry_id = write_inquiry("birthday_party", payload_data())
    return api_success({"inquiry_id": inquiry_id}, 201)


@app.post("/api/inquiries/corporate-orders")
@limiter.limit("5 per hour")
def corporate_inquiry_api():
    inquiry_id = write_inquiry("corporate_order", payload_data())
    return api_success({"inquiry_id": inquiry_id}, 201)


@app.post("/api/inquiries/franchise")
@limiter.limit("5 per hour")
def franchise_inquiry_api():
    inquiry_id = write_inquiry("franchise", payload_data())
    return api_success({"inquiry_id": inquiry_id}, 201)


# =============================================================================
# Bookings / reservations
# =============================================================================

def booking_datetime_local_to_utc(booking_date: date, booking_time: dt_time) -> datetime:
    return datetime.combine(booking_date, booking_time, tzinfo=BUSINESS_TZ).astimezone(timezone.utc)


@app.get("/bookings")
@customer_required
def bookings_page():
    p = current_principal()
    q = firestore_query("bookings", [("customer_uid", "==", p["uid"])])
    items, cursor = paginate_query(
        "bookings", q, order_field="booking_at", limit=DEFAULT_PAGE_SIZE, cursor=request.args.get("cursor")
    )
    return render_page("bookings.html", bookings=items, next_cursor=cursor)


@app.post("/api/bookings")
@limiter.limit("10 per day")
@customer_required
def create_booking_api():
    p = current_principal()
    data = payload_data()
    outlet = get_active_location(str(data.get("outlet_id") or ""), "dine_in")
    booking_date = parse_date_value(data.get("date"), "date", True)
    booking_time = parse_time_value(data.get("time"), "time", True)
    assert booking_date and booking_time
    booking_at = booking_datetime_local_to_utc(booking_date, booking_time)
    if booking_at < utcnow() + timedelta(minutes=30):
        raise AppError("INVALID_BOOKING_TIME", "Booking time must be at least 30 minutes in the future.", 422)
    guests = validate_int(data.get("guests"), "guests", 1, 100)
    phone = validate_phone(data.get("phone"), True)
    notes = validate_text(data.get("notes"), "notes", max_len=800)
    ref = require_db().collection("bookings").document()
    reference = generate_booking_reference()
    ref.set(
        {
            "booking_reference": reference,
            "customer_uid": p["uid"],
            "outlet_id": outlet["id"],
            "date": booking_date.isoformat(),
            "time": booking_time.strftime("%H:%M"),
            "booking_at": booking_at,
            "guests": guests,
            "phone": phone,
            "notes": notes,
            "status": "pending",
            "internal_note": "",
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    create_notification(
        recipient_uid=p["uid"],
        notification_type="transactional",
        title="Booking request received",
        message=f"Your booking request {reference} is pending confirmation.",
    )
    return api_success(get_document("bookings", ref.id, required=True), 201)


@app.get("/api/bookings")
@customer_required
def customer_bookings_api():
    p = current_principal()
    q = firestore_query("bookings", [("customer_uid", "==", p["uid"])])
    items, cursor = paginate_query(
        "bookings", q, order_field="booking_at", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"bookings": items, "next_cursor": cursor})


@app.post("/api/bookings/<booking_id>/cancel")
@customer_required
def customer_cancel_booking_api(booking_id: str):
    p = current_principal()
    booking = get_document("bookings", validate_id(booking_id, "booking_id"), required=True, code="BOOKING_NOT_FOUND")
    if booking.get("customer_uid") != p["uid"]:
        raise AppError("BOOKING_NOT_FOUND", "Booking was not found.", 404)
    if booking.get("status") in {"cancelled", "completed", "rejected"}:
        raise AppError("INVALID_BOOKING_STATUS", "This booking can no longer be cancelled.", 409)
    require_db().collection("bookings").document(booking["id"]).set(
        {"status": "cancelled", "cancelled_at": firestore.SERVER_TIMESTAMP, "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    audit_log("booking.customer_cancelled", "booking", booking["id"])
    return api_success(get_document("bookings", booking["id"], required=True))


# =============================================================================
# Rewards architecture (formula intentionally not invented)
# =============================================================================

@app.get("/api/rewards")
@login_required
def rewards_api():
    p = current_principal()
    account = get_document("rewards_accounts", p["uid"])
    if not account:
        account = {
            "user_id": p["uid"],
            "points": 0,
            "tier": None,
            "program_enabled": False,
            "message": "Rewards formula has not been configured yet.",
        }
    return api_success(account)


# =============================================================================
# Dashboard metrics / staff-safe order views
# =============================================================================

def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day, dt_time.min, tzinfo=BUSINESS_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def aggregate_sum(
    query: Any,
    field: str,
    *,
    fallback_collection: Optional[str] = None,
    fallback_filters: Iterable[tuple[str, str, Any]] = (),
) -> int:
    try:
        aggregate_query = aggregation.AggregationQuery(query)
        aggregate_query.sum(field, alias="sum")
        results = aggregate_query.get()
        if not results:
            return 0
        value = results[0][0].value
        return int(value or 0)
    except FailedPrecondition as exc:
        if not (_is_missing_firestore_index_error(exc) and ALLOW_FIRESTORE_INDEX_FALLBACK):
            raise

        collection = fallback_collection or _query_fallback_collection(query)
        filters = list(fallback_filters)
        if not filters:
            extracted = _query_fallback_filters(query)
            if extracted is not None:
                filters = extracted

        if not collection:
            raise

        logger.warning(
            "Missing Firestore index for aggregate sum on %s; using bounded development fallback.",
            collection,
        )
        total = 0
        for item in _scan_collection_with_filters(collection, filters):
            value = _nested_value(item, field)
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                total += int(value)
        return total


def order_count(filters: list[tuple[str, str, Any]]) -> int:
    return count_query(
        firestore_query("orders", filters),
        fallback_collection="orders",
        fallback_filters=filters,
    )


def booking_count(filters: list[tuple[str, str, Any]]) -> int:
    return count_query(
        firestore_query("bookings", filters),
        fallback_collection="bookings",
        fallback_filters=filters,
    )


def kitchen_order_view(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "order_reference": order.get("order_reference"),
        "service_mode": order.get("service_mode"),
        "outlet_id": order.get("outlet_id"),
        "items": [
            {
                "item_id": item.get("item_id"),
                "item_type": item.get("item_type"),
                "product_id": item.get("product_id"),
                "deal_id": item.get("deal_id"),
                "product_name": item.get("product_name"),
                "deal_name": item.get("deal_name"),
                "quantity": item.get("quantity"),
                "selected_options": item.get("selected_options", []),
                "included_items": item.get("included_items", []),
            }
            for item in order.get("items", [])
        ],
        "customer_notes": order.get("customer_notes", ""),
        "order_status": order.get("order_status"),
        "schedule_for": order.get("schedule_for"),
        "placed_at": order.get("placed_at"),
        "updated_at": order.get("updated_at"),
    }


def staff_order_view(order: dict[str, Any], principal: dict[str, Any]) -> dict[str, Any]:
    if principal.get("role") == ROLE_CHEF:
        return kitchen_order_view(order)
    return order


def dashboard_metrics(principal: dict[str, Any]) -> dict[str, Any]:
    today_local = business_now().date()
    tomorrow_local = today_local + timedelta(days=1)
    today_start, today_end = business_day_bounds(today_local)
    tomorrow_start, tomorrow_end = business_day_bounds(tomorrow_local)

    metrics: dict[str, Any] = {
        "today_orders": order_count([("placed_at", ">=", today_start), ("placed_at", "<", today_end)]),
        "today_completed_orders": order_count(
            [
                ("placed_at", ">=", today_start),
                ("placed_at", "<", today_end),
                ("order_status", "in", ["delivered", "picked_up", "completed"]),
            ]
        ),
        "pending_orders": order_count([("order_status", "in", ["order_placed", "received", "confirmed"])]),
        "preparing_orders": order_count([("order_status", "==", "preparing")]),
        "out_for_delivery_orders": order_count([("order_status", "==", "out_for_delivery")]),
        "cancelled_orders_today": order_count(
            [("placed_at", ">=", today_start), ("placed_at", "<", today_end), ("order_status", "==", "cancelled")]
        ),
        "delivery_order_count_today": order_count(
            [("placed_at", ">=", today_start), ("placed_at", "<", today_end), ("service_mode", "==", "delivery")]
        ),
        "pickup_order_count_today": order_count(
            [("placed_at", ">=", today_start), ("placed_at", "<", today_end), ("service_mode", "==", "pickup")]
        ),
        "dine_in_order_count_today": order_count(
            [("placed_at", ">=", today_start), ("placed_at", "<", today_end), ("service_mode", "==", "dine_in")]
        ),
        "scheduled_today": order_count([("schedule_for", ">=", today_start), ("schedule_for", "<", today_end)]),
        "tomorrow_orders": order_count([("schedule_for", ">=", tomorrow_start), ("schedule_for", "<", tomorrow_end)]),
        "advance_orders": order_count([("schedule_for", ">=", tomorrow_end)]),
        "today_bookings": booking_count([("booking_at", ">=", today_start), ("booking_at", "<", today_end)]),
        "tomorrow_bookings": booking_count([("booking_at", ">=", tomorrow_start), ("booking_at", "<", tomorrow_end)]),
        "upcoming_bookings": booking_count([("booking_at", ">=", utcnow()), ("status", "in", ["pending", "confirmed"])]),
    }

    recent_orders = ordered_documents(
        "orders",
        order_field="placed_at",
        direction="desc",
        limit=10,
    )
    previous_orders = ordered_documents(
        "orders",
        [("placed_at", "<", today_start)],
        order_field="placed_at",
        direction="desc",
        limit=10,
    )
    metrics["recent_orders"] = [staff_order_view(x, principal) for x in recent_orders]
    metrics["previous_orders"] = [staff_order_view(x, principal) for x in previous_orders]

    if principal.get("role") != ROLE_CHEF:
        metrics["recent_reviews"] = ordered_documents(
            "reviews",
            order_field="created_at",
            direction="desc",
            limit=10,
        )
        customer_filters = [("role", "==", ROLE_CUSTOMER), ("status", "==", "active")]
        metrics["customer_count"] = count_query(
            firestore_query("users", customer_filters),
            fallback_collection="users",
            fallback_filters=customer_filters,
        )

        # Legitimate top-selling products only: paid + terminal successful orders, bounded to recent 30 days.
        top_start = utcnow() - timedelta(days=30)
        sold_counter: Counter[str] = Counter()
        name_map: dict[str, str] = {}
        completed_docs = ordered_documents(
            "orders",
            [
                ("payment_status", "==", "paid"),
                ("order_status", "in", ["delivered", "picked_up", "completed"]),
                ("placed_at", ">=", top_start),
            ],
            order_field="placed_at",
            direction="desc",
            limit=500,
        )
        for order in completed_docs:
            for item in order.get("items", []):
                if item.get("item_type") != "product":
                    continue
                pid = str(item.get("product_id") or "")
                if pid:
                    sold_counter[pid] += int(item.get("quantity", 0))
                    name_map[pid] = str(item.get("product_name") or pid)
        metrics["top_selling_products"] = [
            {"product_id": pid, "name": name_map.get(pid, pid), "quantity": qty}
            for pid, qty in sold_counter.most_common(10)
        ]

    can_view_finance = principal.get("role") == ROLE_OWNER or "view_finance" in set(principal.get("permissions", []))
    if can_view_finance:
        sales_filters = [
            ("placed_at", ">=", today_start),
            ("placed_at", "<", today_end),
            ("payment_status", "==", "paid"),
            ("order_status", "!=", "cancelled"),
        ]
        sales_q = firestore_query("orders", sales_filters)
        sales_minor = aggregate_sum(
            sales_q,
            "total_minor",
            fallback_collection="orders",
            fallback_filters=sales_filters,
        )
        paid_count = count_query(
            sales_q,
            fallback_collection="orders",
            fallback_filters=sales_filters,
        )
        metrics["today_sales"] = money_fields(sales_minor)
        metrics["revenue_today"] = money_fields(sales_minor)
        metrics["average_order_value_today"] = money_fields(int(sales_minor / paid_count) if paid_count else 0)
    return metrics



# =============================================================================
# Browser management console (generic existing-template wiring)
# =============================================================================
# These routes intentionally reuse manage_list.html / manage_form.html without
# changing those templates. Mutations continue to go through the existing API
# endpoints below, so the existing validation, permission checks, audit logging,
# Firebase synchronization and Firestore writes remain authoritative.

MANAGEMENT_SCAN_LIMIT = max(50, min(int(os.getenv("MANAGEMENT_SCAN_LIMIT", "250")), 500))

MANAGEMENT_RESOURCE_SPECS: dict[str, dict[str, Any]] = {
    "products": {
        "title": "Products", "description": "Add, edit, publish and temporarily remove menu products.",
        "collection": "products", "view_permission": "view_products", "manage_permission": "manage_products",
        "order_field": "updated_at", "direction": "desc", "icon": "products", "create": True,
        "search_fields": ("name", "slug", "description"),
    },
    "categories": {
        "title": "Categories", "description": "Manage menu sections used by products.",
        "collection": "categories", "view_permission": "view_categories", "manage_permission": "manage_categories",
        "order_field": "display_order", "direction": "asc", "icon": "records", "create": True,
        "search_fields": ("name", "slug", "description"),
    },
    "deals": {
        "title": "Deals", "description": "Manage promotional bundles, prices and visibility.",
        "collection": "deals", "view_permission": "view_deals", "manage_permission": "manage_deals",
        "order_field": "display_order", "direction": "asc", "icon": "deals", "create": True,
        "search_fields": ("name", "description"),
    },
    "locations": {
        "title": "Locations", "description": "Manage restaurant outlets and service availability.",
        "collection": "locations", "view_permission": "view_locations", "manage_permission": "manage_locations",
        "order_field": "display_order", "direction": "asc", "icon": "locations", "create": True,
        "search_fields": ("name", "address", "city", "area"),
    },
    "delivery-pricing": {
        "title": "Delivery Pricing",
        "description": "Set automatic delivery charges by distance, globally or for a specific outlet.",
        "collection": "delivery_pricing_tiers",
        "view_permission": "view_locations",
        "manage_permission": "manage_locations",
        "order_field": "display_order",
        "direction": "asc",
        "icon": "locations",
        "create": True,
        "search_fields": ("label", "outlet_id"),
    },
    "hero-media": {
        "title": "Hero Banners", "description": "Manage homepage image/video banners and source URLs.",
        "collection": "hero_media", "view_permission": "manage_hero_media", "manage_permission": "manage_hero_media",
        "order_field": "display_order", "direction": "asc", "icon": "records", "create": True,
        "search_fields": ("title", "subtitle", "media_type"),
    },
    "orders": {
        "title": "Orders", "description": "Review orders and apply valid server-authorized status transitions.",
        "collection": "orders", "view_permission": "view_orders", "manage_permission": "update_orders",
        "order_field": "placed_at", "direction": "desc", "icon": "orders", "create": False,
        "search_fields": ("order_reference", "customer_name", "customer_email", "order_status"),
    },
    "customers": {
        "title": "Customers", "description": "Review customer accounts and enable or disable access.",
        "collection": "users", "view_permission": "view_customers", "manage_permission": "manage_customers",
        "order_field": "created_at", "direction": "desc", "icon": "customers", "create": False,
        "base_filters": (("role", "==", ROLE_CUSTOMER),), "search_fields": ("full_name", "email", "phone"),
    },
    "staff": {
        "title": "Staff", "description": "Create staff and manage roles, permissions and account status.",
        "collection": "staff", "view_permission": "manage_staff", "manage_permission": "manage_staff",
        "order_field": "created_at", "direction": "desc", "icon": "staff", "create": True,
        "search_fields": ("full_name", "email", "role"),
    },
    "reviews": {
        "title": "Reviews", "description": "Moderate verified-order product reviews.",
        "collection": "reviews", "view_permission": "view_reviews", "manage_permission": "moderate_reviews",
        "order_field": "created_at", "direction": "desc", "icon": "reviews", "create": False,
        "search_fields": ("comment", "moderation_state", "product_id"),
    },
    "payments": {
        "title": "Payments", "description": "Review payments and verify supported manual payment methods.",
        "collection": "payments", "view_permission": "view_payments", "manage_permission": "verify_payments",
        "order_field": "created_at", "direction": "desc", "icon": "payments", "create": False,
        "search_fields": ("order_id", "method", "status", "reference"),
    },
    "bookings": {
        "title": "Bookings", "description": "Manage booking status, schedule, guests and internal notes.",
        "collection": "bookings", "view_permission": "view_bookings", "manage_permission": "manage_bookings",
        "order_field": "booking_at", "direction": "asc", "icon": "records", "create": False,
        "search_fields": ("booking_reference", "name", "email", "phone", "status"),
    },
    "feedback": {
        "title": "Feedback", "description": "Review customer feedback and maintain internal follow-up status.",
        "collection": "feedback", "view_permission": "view_feedback", "manage_permission": "manage_feedback",
        "order_field": "created_at", "direction": "desc", "icon": "feedback", "create": False,
        "search_fields": ("name", "email", "subject", "message", "status"),
    },
    "inquiries": {
        "title": "Inquiries", "description": "Review birthday, corporate-order and franchise inquiries.",
        "collection": "inquiries", "view_permission": "view_feedback", "manage_permission": None,
        "order_field": "created_at", "direction": "desc", "icon": "feedback", "create": False,
        "search_fields": ("name", "email", "phone", "kind", "message"),
    },
    "audit-logs": {
        "title": "Audit Logs", "description": "Read recent administrative activity recorded by the backend.",
        "collection": "audit_logs", "view_permission": "view_reports", "manage_permission": None,
        "order_field": "created_at", "direction": "desc", "icon": "records", "create": False,
        "search_fields": ("action", "target_type", "target_id", "actor_role"),
    },
}


def _management_has_permission(principal: dict[str, Any], permission: Optional[str]) -> bool:
    if not permission:
        return False
    return principal.get("role") == ROLE_OWNER or permission in set(principal.get("permissions", []))


def _management_spec(resource_key: str, *, write: bool = False) -> dict[str, Any]:
    key = str(resource_key or "").strip().lower()
    spec = MANAGEMENT_RESOURCE_SPECS.get(key)
    if spec is None:
        raise AppError("NOT_FOUND", "The requested management resource was not found.", 404)
    principal = current_principal()
    required = spec.get("manage_permission") if write else spec.get("view_permission")
    if not _management_has_permission(principal, required):
        raise AppError("ACCESS_DENIED", "You do not have permission to access this management resource.", 403)
    return spec




def _management_singular_title(resource_key: str, spec: dict[str, Any]) -> str:
    special = {"categories": "Category", "hero-media": "Hero Banner", "delivery-pricing": "Delivery Price Tier"}
    if resource_key in special:
        return special[resource_key]
    title = str(spec.get("title") or "Record")
    return title[:-1] if title.endswith("s") else title

def _management_url(resource_key: str, record_id: Optional[str] = None, *, mode: str = "list") -> str:
    if mode == "list":
        return url_for("manage_resource_list", resource_key=resource_key)
    if mode == "new":
        return url_for("manage_resource_create_page", resource_key=resource_key)
    if mode == "edit" and record_id:
        return url_for("manage_resource_edit_page", resource_key=resource_key, record_id=record_id)
    return url_for("manage_home")


def _management_datetime_local(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(BUSINESS_TZ).strftime("%Y-%m-%dT%H:%M")


def _management_datetime_label(value: Any) -> str:
    if not isinstance(value, datetime):
        return str(value or "")
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M")


def _management_text_match(item: dict[str, Any], fields: Iterable[str], query: str) -> bool:
    needle = normalize_search(query)
    if not needle:
        return True
    for field in fields:
        value = _nested_value(item, field)
        if value is None:
            continue
        if needle in normalize_search(str(value)):
            return True
    return False


def _management_filter_value(item: dict[str, Any], name: str, value: str) -> bool:
    if value == "":
        return True
    actual = _nested_value(item, name)
    if value in {"true", "false"} and isinstance(actual, bool):
        return actual is (value == "true")
    return str(actual or "").lower() == value.lower()


def _management_options(items: Iterable[dict[str, Any]], *, label_key: str = "name", value_key: str = "id") -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in items:
        value = str(item.get(value_key) or "").strip()
        if not value:
            continue
        label = str(item.get(label_key) or value).strip()
        result.append({"value": value, "label": label})
    return result


def _management_active_categories() -> list[dict[str, Any]]:
    items = _scan_collection_with_filters("categories", (), scan_limit=200)
    items = [x for x in items if x.get("active", False)]
    items.sort(key=lambda x: _python_sort_key(x.get("display_order")))
    return items


def _management_active_locations() -> list[dict[str, Any]]:
    items = _scan_collection_with_filters("locations", (), scan_limit=200)
    items = [x for x in items if x.get("active", False)]
    items.sort(key=lambda x: _python_sort_key(x.get("display_order")))
    return items


def _management_active_products() -> list[dict[str, Any]]:
    items = _scan_collection_with_filters("products", (), scan_limit=300)
    items = [x for x in items if x.get("active", False)]
    items.sort(key=lambda x: _python_sort_key(x.get("name_search") or x.get("name")))
    return items


def _management_list_filters(resource_key: str) -> list[dict[str, Any]]:
    yes_no = [{"value": "true", "label": "Yes"}, {"value": "false", "label": "No"}]
    if resource_key in {"products", "categories", "deals", "locations", "hero-media", "delivery-pricing"}:
        filters: list[dict[str, Any]] = [{"name": "active", "label": "Active", "type": "select", "options": yes_no}]
        if resource_key == "products":
            filters.append({"name": "available", "label": "Available", "type": "select", "options": yes_no})
            filters.append({"name": "category_id", "label": "Category", "type": "select", "options": _management_options(_management_active_categories())})
        if resource_key == "hero-media":
            filters.append({"name": "media_type", "label": "Media Type", "type": "select", "options": [{"value": "image", "label": "Image"}, {"value": "video", "label": "Video"}]})
        if resource_key == "delivery-pricing":
            filters.append(
                {
                    "name": "outlet_id",
                    "label": "Outlet",
                    "type": "select",
                    "options": [{"value": "", "label": "All / Global"}] + _management_options(_management_active_locations()),
                }
            )
        return filters
    if resource_key in {"customers", "staff"}:
        filters = [{"name": "status", "label": "Status", "type": "select", "options": [{"value": "active", "label": "Active"}, {"value": "disabled", "label": "Disabled"}]}]
        if resource_key == "staff":
            filters.append({"name": "role", "label": "Role", "type": "select", "options": [{"value": r, "label": r.replace("_", " ").title()} for r in sorted(STAFF_ROLES)]})
        return filters
    if resource_key == "reviews":
        return [{"name": "moderation_state", "label": "Moderation", "type": "select", "options": [{"value": s, "label": s.title()} for s in sorted(REVIEW_MODERATION_STATES)]}]
    if resource_key == "payments":
        return [
            {"name": "status", "label": "Status", "type": "select", "options": [{"value": s, "label": s.replace("_", " ").title()} for s in sorted(PAYMENT_STATUSES)]},
            {"name": "method", "label": "Method", "type": "select", "options": [{"value": m, "label": m.replace("_", " ").title()} for m in sorted(PAYMENT_METHODS)]},
        ]
    if resource_key == "bookings":
        return [{"name": "status", "label": "Status", "type": "select", "options": [{"value": s, "label": s.title()} for s in sorted(BOOKING_STATUSES)]}]
    if resource_key == "orders":
        all_statuses = sorted(set(DELIVERY_STATUSES + PICKUP_STATUSES + DINE_IN_STATUSES))
        return [
            {"name": "order_status", "label": "Order Status", "type": "select", "options": [{"value": s, "label": s.replace("_", " ").title()} for s in all_statuses]},
            {"name": "service_mode", "label": "Service", "type": "select", "options": [{"value": m, "label": m.replace("_", " ").title()} for m in sorted(SERVICE_MODES)]},
            {"name": "payment_status", "label": "Payment", "type": "select", "options": [{"value": s, "label": s.replace("_", " ").title()} for s in sorted(PAYMENT_STATUSES)]},
        ]
    if resource_key == "inquiries":
        return [{"name": "kind", "label": "Kind", "type": "text"}]
    if resource_key == "feedback":
        return [{"name": "status", "label": "Status", "type": "text"}]
    return []


def _management_columns(resource_key: str) -> list[dict[str, Any]]:
    columns: dict[str, list[dict[str, Any]]] = {
        "products": [
            {"key": "image_url", "label": "Image", "type": "image"}, {"key": "name", "label": "Product"},
            {"key": "category_name", "label": "Category"}, {"key": "price", "label": "Price", "type": "money"},
            {"key": "available", "label": "Available", "type": "boolean"}, {"key": "active", "label": "Active", "type": "boolean"},
        ],
        "categories": [{"key": "image_url", "label": "Image", "type": "image"}, {"key": "name", "label": "Category"}, {"key": "slug", "label": "Slug"}, {"key": "display_order", "label": "Order", "type": "number"}, {"key": "active", "label": "Active", "type": "boolean"}],
        "deals": [{"key": "image_url", "label": "Image", "type": "image"}, {"key": "name", "label": "Deal"}, {"key": "price", "label": "Price", "type": "money"}, {"key": "featured", "label": "Featured", "type": "boolean"}, {"key": "active", "label": "Active", "type": "boolean"}],
        "locations": [{"key": "name", "label": "Location"}, {"key": "city", "label": "City"}, {"key": "area", "label": "Area"}, {"key": "delivery_enabled", "label": "Delivery", "type": "boolean"}, {"key": "active", "label": "Active", "type": "boolean"}],
        "delivery-pricing": [
            {"key": "label", "label": "Tier"},
            {"key": "outlet_name", "label": "Outlet"},
            {"key": "distance_range", "label": "Distance"},
            {"key": "fee", "label": "Charge", "type": "money"},
            {"key": "active", "label": "Active", "type": "boolean"},
        ],
        "hero-media": [{"key": "media_type", "label": "Type", "type": "status"}, {"key": "title", "label": "Title"}, {"key": "media_url", "label": "Source", "truncate": 55}, {"key": "display_order", "label": "Order", "type": "number"}, {"key": "active", "label": "Active", "type": "boolean"}],
        "orders": [{"key": "order_reference", "label": "Order"}, {"key": "service_mode", "label": "Service", "type": "status"}, {"key": "order_status", "label": "Status", "type": "status"}, {"key": "payment_status", "label": "Payment", "type": "status"}, {"key": "total", "label": "Total", "type": "money"}, {"key": "placed_at_label", "label": "Placed", "type": "datetime"}],
        "customers": [{"key": "full_name", "label": "Customer"}, {"key": "email", "label": "Email"}, {"key": "phone", "label": "Phone"}, {"key": "status", "label": "Status", "type": "status"}, {"key": "created_at_label", "label": "Joined", "type": "datetime"}],
        "staff": [{"key": "full_name", "label": "Staff"}, {"key": "email", "label": "Email"}, {"key": "role", "label": "Role", "type": "status"}, {"key": "status", "label": "Status", "type": "status"}, {"key": "permission_count", "label": "Permissions", "type": "count"}],
        "reviews": [{"key": "rating", "label": "Rating", "type": "rating"}, {"key": "comment", "label": "Comment", "truncate": 85}, {"key": "moderation_state", "label": "State", "type": "status"}, {"key": "created_at_label", "label": "Created", "type": "datetime"}],
        "payments": [{"key": "order_id", "label": "Order ID"}, {"key": "method", "label": "Method", "type": "status"}, {"key": "status", "label": "Status", "type": "status"}, {"key": "amount", "label": "Amount", "type": "money"}, {"key": "created_at_label", "label": "Created", "type": "datetime"}],
        "bookings": [{"key": "booking_reference", "label": "Booking"}, {"key": "name", "label": "Guest"}, {"key": "date", "label": "Date"}, {"key": "time", "label": "Time"}, {"key": "guests", "label": "Guests", "type": "count"}, {"key": "status", "label": "Status", "type": "status"}],
        "feedback": [{"key": "name", "label": "Name"}, {"key": "email", "label": "Email"}, {"key": "subject", "label": "Subject"}, {"key": "message", "label": "Message", "truncate": 75}, {"key": "status", "label": "Status", "type": "status"}],
        "inquiries": [{"key": "kind", "label": "Kind", "type": "status"}, {"key": "name", "label": "Name"}, {"key": "email", "label": "Email"}, {"key": "phone", "label": "Phone"}, {"key": "created_at_label", "label": "Created", "type": "datetime"}],
        "audit-logs": [{"key": "action", "label": "Action"}, {"key": "actor_role", "label": "Actor Role", "type": "status"}, {"key": "target_type", "label": "Target"}, {"key": "target_id", "label": "Target ID"}, {"key": "created_at_label", "label": "Created", "type": "datetime"}],
    }
    return columns.get(resource_key, [{"key": "id", "label": "ID"}])


def _management_enrich_row(resource_key: str, item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row["created_at_label"] = _management_datetime_label(row.get("created_at"))
    row["updated_at_label"] = _management_datetime_label(row.get("updated_at"))
    if resource_key == "products":
        category = get_document("categories", str(row.get("category_id") or "")) if row.get("category_id") else None
        row["category_name"] = (category or {}).get("name", "—")
        row["price"] = money_fields(int(row.get("base_price_minor") or 0))
    elif resource_key == "delivery-pricing":
        outlet_id = str(row.get("outlet_id") or "")
        outlet = get_document("locations", outlet_id) if outlet_id else None
        row["name"] = str(row.get("label") or "Delivery Price Tier")
        row["outlet_name"] = (outlet or {}).get("name", "All Outlets")
        row["distance_range"] = f"{float(row.get('min_distance_km') or 0):g}–{float(row.get('max_distance_km') or 0):g} km"
        row["fee"] = money_fields(int(row.get("fee_minor") or 0))
    elif resource_key == "deals":
        row["price"] = money_fields(int(row.get("price_minor") or 0))
    elif resource_key == "orders":
        row["total"] = money_fields(int(row.get("total_minor") or 0))
        row["placed_at_label"] = _management_datetime_label(row.get("placed_at"))
    elif resource_key == "payments":
        amount_minor = row.get("amount_minor")
        if amount_minor is None:
            amount_minor = row.get("total_minor", 0)
        row["amount"] = money_fields(int(amount_minor or 0))
    elif resource_key == "staff":
        row["permission_count"] = len(row.get("permissions") or [])
    return row


def _management_row_actions(resource_key: str, row: dict[str, Any], principal: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    rid = str(row.get("id") or row.get("uid") or "").strip()
    if not rid:
        return []
    can_manage = _management_has_permission(principal, spec.get("manage_permission"))
    actions: list[dict[str, Any]] = []

    if resource_key == "products" and can_manage:
        actions.extend([
            {"label": "Edit", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"},
            {"label": "Options / Variants", "url": url_for("manage_product_options_page", product_id=rid), "method": "GET", "style": "secondary"},
        ])
        gallery_template_path = os.path.join(app.root_path, app.template_folder or "templates", "product_gallery_admin.html")
        if os.path.isfile(gallery_template_path):
            actions.append(
                {"label": "Gallery", "url": url_for("manage_product_gallery_page", product_id=rid), "method": "GET", "style": "secondary"}
            )
        if row.get("active", False):
            actions.append({"label": "Remove from Menu", "url": f"/api/admin/products/{rid}/deactivate", "method": "POST", "style": "warning", "confirm": {"title": "Remove product from menu?", "message": "The product will be hidden without deleting order history.", "confirm_label": "Remove"}})
        else:
            actions.append({"label": "Restore to Menu", "url": f"/api/admin/products/{rid}/reactivate", "method": "POST", "style": "primary"})
        actions.append({"label": "Public Preview", "url": url_for("product_page", product_id=rid), "method": "GET", "style": "neutral"})
    elif resource_key in {"categories", "deals", "locations", "hero-media", "delivery-pricing"} and can_manage:
        actions.append({"label": "Edit", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"})
        api_segment = {"hero-media": "hero-media", "delivery-pricing": "delivery-pricing"}.get(resource_key, resource_key)
        if row.get("active", False):
            actions.append({"label": "Deactivate", "url": f"/api/admin/{api_segment}/{rid}/deactivate", "method": "POST", "style": "warning", "confirm": {"title": "Deactivate record?", "message": "This record will stop appearing in active public content.", "confirm_label": "Deactivate"}})
        else:
            actions.append({"label": "Reactivate", "url": f"/api/admin/{api_segment}/{rid}/reactivate", "method": "POST", "style": "primary"})
    elif resource_key == "customers" and can_manage:
        actions.append({"label": "Manage Status", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"})
    elif resource_key == "staff" and can_manage:
        # Owner cannot edit itself through the staff API; the API remains the final authority.
        if rid != principal.get("uid"):
            actions.append({"label": "Edit Access", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"})
            target_status = "disabled" if row.get("status", "active") == "active" else "active"
            actions.append({"label": "Disable" if target_status == "disabled" else "Enable", "url": f"/api/admin/staff/{rid}/status", "method": "PATCH", "style": "warning" if target_status == "disabled" else "primary", "payload": {"status": target_status}, "confirm": {"title": "Change staff account status?", "message": "Firebase Authentication and trusted staff records will be updated together.", "confirm_label": "Continue"}})
    elif resource_key == "reviews" and can_manage:
        actions.append({"label": "Moderate", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"})
    elif resource_key == "payments" and can_manage:
        method = row.get("method")
        status = row.get("status")
        if method == "bank_transfer" and status in {"pending", "awaiting_verification"}:
            actions.append({"label": "Verify", "url": f"/api/admin/payments/{rid}/action", "method": "POST", "style": "primary", "payload": {"action": "verify_bank"}, "confirm": {"title": "Verify bank transfer?", "message": "This will mark the payment and linked order as paid.", "confirm_label": "Verify", "note_field": "note", "note_label": "Verification note", "note_max_length": 500}})
            actions.append({"label": "Reject", "url": f"/api/admin/payments/{rid}/action", "method": "POST", "style": "danger", "payload": {"action": "reject_bank"}, "confirm": {"title": "Reject bank transfer?", "message": "The linked order will be placed on a payment-rejected hold.", "confirm_label": "Reject", "note_field": "note", "note_label": "Reason", "note_required": True, "note_max_length": 500}})
        elif method == "cod" and status == "pending":
            actions.append({"label": "Mark COD Paid", "url": f"/api/admin/payments/{rid}/action", "method": "POST", "style": "primary", "payload": {"action": "mark_cod_paid"}, "confirm": {"title": "Mark COD payment paid?", "message": "Use this only after cash has been collected.", "confirm_label": "Mark Paid", "note_field": "note", "note_label": "Note", "note_max_length": 500}})
    elif resource_key == "bookings" and can_manage:
        actions.append({"label": "Edit", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"})
    elif resource_key == "feedback" and can_manage:
        actions.append({"label": "Update", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"})
    elif resource_key == "orders" and can_manage:
        possible = set(ORDER_TRANSITIONS.get(row.get("service_mode"), {}).get(row.get("order_status"), set()))
        if principal.get("role") == ROLE_CHEF:
            possible &= CHEF_ALLOWED_STATUS_TARGETS
        if not _management_has_permission(principal, "cancel_orders"):
            possible.discard("cancelled")
        if possible:
            actions.append({"label": "Update Status", "url": _management_url(resource_key, rid, mode="edit"), "method": "GET", "style": "primary"})
    return actions


def _management_fetch_rows(resource_key: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    sort_options = _management_sort_options(resource_key, spec)
    allowed_sort = {x["key"]: x["field"] for x in sort_options}
    requested_sort = request.args.get("sort", "").strip()
    order_field = allowed_sort.get(requested_sort, spec.get("order_field", "updated_at"))
    direction = request.args.get("direction", spec.get("direction", "desc")).strip().lower()
    if direction not in {"asc", "desc"}:
        direction = str(spec.get("direction", "desc"))
    items = _scan_collection_with_filters(spec["collection"], (), scan_limit=MANAGEMENT_SCAN_LIMIT)
    for field, op, expected in spec.get("base_filters", ()):
        items = [item for item in items if _matches_filter(item, field, op, expected)]
    items.sort(
        key=lambda item: _python_sort_key(_nested_value(item, order_field)),
        reverse=direction == "desc",
    )

    filters = _management_list_filters(resource_key)
    for filter_def in filters:
        name = str(filter_def.get("name") or "")
        value = request.args.get(name, "").strip()
        if value:
            items = [item for item in items if _management_filter_value(item, name, value)]

    query = request.args.get("q", "").strip()
    if query:
        items = [item for item in items if _management_text_match(item, spec.get("search_fields", ()), query)]

    principal = current_principal()
    rows = []
    for item in items:
        row = _management_enrich_row(resource_key, item)
        row["actions"] = _management_row_actions(resource_key, row, principal, spec)
        rows.append(row)
    return rows


def _management_sort_options(resource_key: str, spec: dict[str, Any]) -> list[dict[str, str]]:
    by_resource: dict[str, list[tuple[str, str, str]]] = {
        "products": [("updated_at", "Updated", "updated_at"), ("name", "Name", "name_search"), ("price", "Price", "base_price_minor"), ("created_at", "Created", "created_at")],
        "categories": [("display_order", "Display Order", "display_order"), ("name", "Name", "name_search")],
        "deals": [("display_order", "Display Order", "display_order"), ("updated_at", "Updated", "updated_at")],
        "locations": [("display_order", "Display Order", "display_order"), ("name", "Name", "name")],
        "delivery-pricing": [("display_order", "Display Order", "display_order"), ("distance", "Distance", "min_distance_km"), ("fee", "Charge", "fee_minor")],
        "hero-media": [("display_order", "Display Order", "display_order"), ("updated_at", "Updated", "updated_at")],
        "orders": [("placed_at", "Placed", "placed_at"), ("updated_at", "Updated", "updated_at"), ("total", "Total", "total_minor"), ("status", "Status", "order_status")],
    }
    entries = by_resource.get(resource_key)
    if entries is None:
        entries = [(str(spec.get("order_field", "updated_at")), "Default", str(spec.get("order_field", "updated_at")))]
    return [{"key": key, "label": label, "field": field} for key, label, field in entries]


def _management_list_payload(resource_key: str) -> dict[str, Any]:
    spec = _management_spec(resource_key)
    principal = current_principal()
    rows = _management_fetch_rows(resource_key, spec)
    can_manage = _management_has_permission(principal, spec.get("manage_permission"))
    create_allowed = bool(spec.get("create") and can_manage)
    if resource_key == "staff" and principal.get("role") not in {ROLE_OWNER, ROLE_SUPER_ADMIN}:
        create_allowed = False
    sort_options = _management_sort_options(resource_key, spec)
    sort_ui = [{"key": x["key"], "label": x["label"]} for x in sort_options]
    current_sort = request.args.get("sort", "").strip() or sort_options[0]["key"]
    return {
        "key": resource_key,
        "title": spec["title"],
        "description": spec["description"],
        "authorized": True,
        "icon": spec.get("icon", resource_key),
        "columns": _management_columns(resource_key),
        "rows": rows,
        "filters": _management_list_filters(resource_key),
        "search": {"enabled": bool(spec.get("search_fields")), "param": "q", "value": request.args.get("q", ""), "placeholder": f"Search {spec['title'].lower()}"},
        "sort_options": sort_ui,
        "current_sort": current_sort,
        "current_direction": request.args.get("direction", spec.get("direction", "desc")),
        "result_count": len(rows),
        "timezone_label": BUSINESS_TIMEZONE_NAME,
        "create_action": {"label": f"Add {_management_singular_title(resource_key, spec)}", "url": _management_url(resource_key, mode="new")} if create_allowed else {},
        "empty_message": f"No {spec['title'].lower()} are available yet.",
        "filtered_empty_message": "No records match the current filters.",
        "id_key": "id",
        "display_key": "name",
        "row_actions_key": "actions",
    }


def _management_form_record(resource_key: str, record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    if resource_key == "products":
        out["base_price"] = minor_to_money(int(record.get("base_price_minor") or 0))
    elif resource_key == "delivery-pricing":
        out["fee"] = minor_to_money(int(record.get("fee_minor") or 0))
    elif resource_key == "deals":
        out["price"] = minor_to_money(int(record.get("price_minor") or 0))
        out["start_at"] = _management_datetime_local(record.get("start_at"))
        out["end_at"] = _management_datetime_local(record.get("end_at"))
    elif resource_key == "hero-media":
        out["start_at"] = _management_datetime_local(record.get("start_at"))
        out["end_at"] = _management_datetime_local(record.get("end_at"))
    return out


def _management_product_fields(record: dict[str, Any]) -> list[dict[str, Any]]:
    category_options = _management_options(_management_active_categories())
    return [
        {"name": "name", "label": "Product Title", "type": "text", "required": True, "min_length": 2, "max_length": 160, "width": "half"},
        {"name": "slug", "label": "Slug", "type": "slug", "max_length": 180, "width": "half", "help": "Leave blank when creating to derive it from the product title."},
        {"name": "description", "label": "Description", "type": "textarea", "max_length": 3000, "width": "full"},
        {"name": "category_id", "label": "Category", "type": "select", "required": True, "empty_label": "Choose a category", "options": category_options, "width": "half"},
        {"name": "base_price", "label": "Base Price (PKR)", "type": "money", "required": True, "min": 0, "step": "0.01", "width": "half"},
        {"name": "image_url", "label": "Product Image URL", "type": "image_url", "width": "full", "help": "Optional URL fallback. If you select files below, Picture 1 becomes the main/cover image automatically."},
        {
            "name": PRODUCT_IMAGE_UPLOAD_FIELD,
            "label": "Select Product Images",
            "type": "file",
            "width": "full",
            "accept": "image/jpeg,image/png,image/webp",
            "multiple": True,
            "image_preview": True,
            "max_files": MAX_PRODUCT_UPLOAD_FILES,
            "max_file_bytes": MAX_PRODUCT_IMAGE_BYTES,
            "help": f"Select up to {MAX_PRODUCT_UPLOAD_FILES} JPG, PNG, or WebP images from your computer. Picture 1 becomes the cover; the rest become the ordered gallery. Each image may be up to {MAX_PRODUCT_IMAGE_UPLOAD_MB} MB.",
        },
        {"name": "active", "label": "Active", "type": "switch", "toggle_label": "Product is active", "width": "third"},
        {"name": "available", "label": "Available", "type": "switch", "toggle_label": "Available for ordering", "width": "third"},
        {"name": "featured", "label": "Featured", "type": "switch", "toggle_label": "Show as featured", "width": "third"},
        {"name": "popular", "label": "Popular", "type": "switch", "toggle_label": "Mark as popular", "width": "third"},
        {"name": "spicy", "label": "Spicy", "type": "switch", "toggle_label": "Spicy item", "width": "third"},
        {"name": "vegetarian", "label": "Vegetarian", "type": "switch", "toggle_label": "Vegetarian item", "width": "third"},
        {"name": "display_order", "label": "Display Order", "type": "integer", "min": 0, "max": 100000, "width": "half"},
    ]


def _management_category_fields() -> list[dict[str, Any]]:
    return [
        {"name": "name", "label": "Category Name", "type": "text", "required": True, "min_length": 2, "max_length": 100},
        {"name": "slug", "label": "Slug", "type": "slug", "max_length": 140, "help": "Leave blank on create to derive it from the name."},
        {"name": "description", "label": "Description", "type": "textarea", "max_length": 1000, "width": "full"},
        {"name": "image_url", "label": "Image URL", "type": "image_url", "width": "full"},
        {"name": "icon", "label": "Icon / Icon Reference", "type": "text", "max_length": 200},
        {"name": "display_order", "label": "Display Order", "type": "integer", "min": 0, "max": 10000},
        {"name": "active", "label": "Active", "type": "switch", "toggle_label": "Category is active"},
    ]


def _management_deal_fields(record: dict[str, Any]) -> list[dict[str, Any]]:
    product_options = _management_options(_management_active_products())
    return [
        {"name": "name", "label": "Deal Title", "type": "text", "required": True, "min_length": 2, "max_length": 160},
        {"name": "price", "label": "Deal Price (PKR)", "type": "money", "required": True, "min": 0, "step": "0.01"},
        {"name": "description", "label": "Description", "type": "textarea", "max_length": 3000, "width": "full"},
        {"name": "image_url", "label": "Deal Image URL", "type": "image_url", "width": "full"},
        {"name": "included_items", "label": "Included Products", "type": "repeater", "value": record.get("included_items", []), "width": "full", "min_items": 0, "max_items": 50, "add_label": "Add Product", "fields": [
            {"name": "product_id", "label": "Product", "type": "select", "required": True, "options": product_options},
            {"name": "quantity", "label": "Quantity", "type": "integer", "required": True, "min": 1, "max": 20, "default": 1},
            {"name": "allow_customization", "label": "Customization", "type": "switch", "toggle_label": "Allow customization", "default": False},
        ]},
        {"name": "featured", "label": "Featured", "type": "switch", "toggle_label": "Feature this deal"},
        {"name": "active", "label": "Active", "type": "switch", "toggle_label": "Deal is active"},
        {"name": "display_order", "label": "Display Order", "type": "integer", "min": 0, "max": 100000},
        {"name": "start_at", "label": "Starts At", "type": "datetime"},
        {"name": "end_at", "label": "Ends At", "type": "datetime"},
    ]


def _management_location_fields() -> list[dict[str, Any]]:
    return [
        {"name": "name", "label": "Location Name", "type": "text", "required": True, "max_length": 120},
        {"name": "phone", "label": "Phone", "type": "tel", "max_length": 24},
        {"name": "address", "label": "Address", "type": "textarea", "required": True, "max_length": 400, "width": "full"},
        {"name": "city", "label": "City", "type": "text", "required": True, "max_length": 100},
        {"name": "area", "label": "Area", "type": "text", "max_length": 120},
        {"name": "latitude", "label": "Latitude", "type": "decimal", "min": -90, "max": 90, "step": "0.0000001"},
        {"name": "longitude", "label": "Longitude", "type": "decimal", "min": -180, "max": 180, "step": "0.0000001"},
        {"name": "delivery_radius_km", "label": "Delivery Radius (km)", "type": "decimal", "min": 0.01, "max": 500, "step": "0.1"},
        {"name": "image_url", "label": "Location Image URL", "type": "image_url", "width": "full"},
        {"name": "delivery_enabled", "label": "Delivery", "type": "switch", "toggle_label": "Delivery enabled"},
        {"name": "pickup_enabled", "label": "Pickup", "type": "switch", "toggle_label": "Pickup enabled"},
        {"name": "dine_in_enabled", "label": "Dine In", "type": "switch", "toggle_label": "Dine-in enabled"},
        {"name": "active", "label": "Active", "type": "switch", "toggle_label": "Location is active"},
        {"name": "display_order", "label": "Display Order", "type": "integer", "min": 0, "max": 100000},
    ]


def _management_delivery_pricing_fields() -> list[dict[str, Any]]:
    outlet_options = [{"value": "", "label": "All Outlets (Global Tier)"}] + _management_options(
        _management_active_locations()
    )
    return [
        {
            "name": "label",
            "label": "Tier Label",
            "type": "text",
            "required": True,
            "max_length": 120,
            "help": "Example: 0–5 km or Nearby Delivery.",
        },
        {
            "name": "outlet_id",
            "label": "Outlet",
            "type": "select",
            "empty_label": "All Outlets (Global Tier)",
            "options": outlet_options,
            "help": "Leave global to use this tier for every outlet without an outlet-specific match.",
        },
        {
            "name": "min_distance_km",
            "label": "From Distance (km)",
            "type": "decimal",
            "required": True,
            "min": 0,
            "max": 500,
            "step": "0.1",
        },
        {
            "name": "max_distance_km",
            "label": "To Distance (km)",
            "type": "decimal",
            "required": True,
            "min": 0.1,
            "max": 500,
            "step": "0.1",
        },
        {
            "name": "fee",
            "label": "Delivery Charge (PKR)",
            "type": "money",
            "required": True,
            "min": 0,
            "step": "0.01",
        },
        {
            "name": "active",
            "label": "Active",
            "type": "switch",
            "toggle_label": "Use this delivery price tier",
        },
        {
            "name": "display_order",
            "label": "Display Order",
            "type": "integer",
            "min": 0,
            "max": 100000,
        },
    ]


def _management_hero_fields() -> list[dict[str, Any]]:
    return [
        {"name": "media_type", "label": "Media Type", "type": "select", "required": True, "options": [{"value": "image", "label": "Image"}, {"value": "video", "label": "Video"}]},
        {"name": "media_url", "label": "Desktop / Main Source URL", "type": "url", "required": True, "width": "full"},
        {"name": "mobile_media_url", "label": "Mobile Source URL", "type": "url", "width": "full"},
        {"name": "poster_url", "label": "Video Poster / Fallback URL", "type": "image_url", "required": True, "width": "full", "condition": {"field": "media_type", "operator": "equals", "value": "video"}, "help": "Required for video banners."},
        {"name": "title", "label": "Banner Title", "type": "text", "max_length": 180},
        {"name": "subtitle", "label": "Subtitle", "type": "textarea", "max_length": 500, "width": "full"},
        {"name": "cta_text", "label": "CTA Text", "type": "text", "max_length": 100},
        {"name": "cta_url", "label": "CTA URL", "type": "text", "max_length": 2048, "help": "May be an internal path such as /menu or an http(s) URL."},
        {"name": "display_order", "label": "Display Order", "type": "integer", "min": 0, "max": 100000},
        {"name": "active", "label": "Active", "type": "switch", "toggle_label": "Banner is active"},
        {"name": "start_at", "label": "Starts At", "type": "datetime"},
        {"name": "end_at", "label": "Ends At", "type": "datetime"},
    ]


def _management_staff_fields(principal: dict[str, Any], *, create: bool) -> list[dict[str, Any]]:
    role_options = [{"value": role, "label": role.replace("_", " ").title()} for role in sorted(allowed_staff_roles_for_actor(principal.get("role", "")))]
    fields = [
        {"name": "full_name", "label": "Full Name", "type": "text", "required": True, "min_length": 2, "max_length": 120},
        {"name": "role", "label": "Role", "type": "select", "required": True, "empty_label": "Choose a role", "options": role_options},
        {"name": "permissions", "label": "Permissions", "type": "multiselect", "required": True, "size": 12, "options": [{"value": p, "label": p.replace("_", " ").title()} for p in sorted(PERMISSIONS)], "width": "full", "help": "The backend limits delegated permissions according to the selected role and acting staff member."},
    ]
    if create:
        fields.insert(1, {"name": "email", "label": "Email", "type": "email", "required": True, "max_length": 254})
    return fields


def _management_form_payload(resource_key: str, record_id: Optional[str] = None) -> dict[str, Any]:
    creating = not record_id
    spec = _management_spec(resource_key, write=True)
    if creating and not spec.get("create"):
        raise AppError("METHOD_NOT_ALLOWED", "This management resource cannot be created from the browser console.", 405)
    principal = current_principal()
    record: dict[str, Any] = {}
    if not creating:
        rid = validate_id(record_id, "record_id")
        record = get_document(spec["collection"], rid, required=True, code="NOT_FOUND")
        if resource_key == "customers" and record.get("role") != ROLE_CUSTOMER:
            raise AppError("NOT_FOUND", "Customer was not found.", 404)
        if resource_key == "staff" and rid == principal.get("uid"):
            raise AppError("SELF_PRIVILEGE_CHANGE_DENIED", "Use your profile for personal details; staff access cannot be changed on your own account.", 403)
        record = _management_form_record(resource_key, record)

    list_url = _management_url(resource_key)
    common: dict[str, Any] = {
        "resource_key": resource_key,
        "resource_title": spec["title"],
        "record": record,
        "record_id": record.get("id", record_id or ""),
        "mode": "create" if creating else "edit",
        "title": ("Add " if creating else "Edit ") + _management_singular_title(resource_key, spec),
        "description": spec["description"],
        "list_url": list_url,
        "success_url": list_url,
        "ajax": True,
        "timezone_label": BUSINESS_TIMEZONE_NAME,
    }

    rid = str(record.get("id") or record_id or "")
    if resource_key == "products":
        product_actions: list[dict[str, Any]] = []
        if not creating and rid:
            options_template_path = os.path.join(
                app.root_path, app.template_folder or "templates", "product_options.html"
            )
            gallery_template_path = os.path.join(
                app.root_path, app.template_folder or "templates", "product_gallery_admin.html"
            )
            if os.path.isfile(options_template_path):
                product_actions.append(
                    {
                        "label": "Options / Variants",
                        "url": url_for("manage_product_options_page", product_id=rid),
                        "method": "GET",
                        "style": "secondary",
                    }
                )
            if os.path.isfile(gallery_template_path):
                product_actions.append(
                    {
                        "label": "Gallery",
                        "url": url_for("manage_product_gallery_page", product_id=rid),
                        "method": "GET",
                        "style": "secondary",
                    }
                )
        common.update({
            "fields": _management_product_fields(record),
            "submit_url": "/api/admin/products" if creating else f"/api/admin/products/{rid}",
            "submit_method": "POST" if creating else "PATCH",
            "submit_label": "Add Product" if creating else "Save Product",
            "public_preview_url": url_for("product_page", product_id=rid) if rid else "",
            "actions": product_actions,
            "upload": {"enabled": True},
        })
    elif resource_key == "categories":
        common.update({"fields": _management_category_fields(), "submit_url": "/api/admin/categories" if creating else f"/api/admin/categories/{rid}", "submit_method": "POST" if creating else "PATCH", "submit_label": "Add Category" if creating else "Save Category"})
    elif resource_key == "deals":
        common.update({"fields": _management_deal_fields(record), "submit_url": "/api/admin/deals" if creating else f"/api/admin/deals/{rid}", "submit_method": "POST" if creating else "PATCH", "submit_label": "Add Deal" if creating else "Save Deal", "public_preview_url": url_for("deal_detail_page", deal_id=rid) if rid else ""})
    elif resource_key == "locations":
        common.update({"fields": _management_location_fields(), "submit_url": "/api/admin/locations" if creating else f"/api/admin/locations/{rid}", "submit_method": "POST" if creating else "PATCH", "submit_label": "Add Location" if creating else "Save Location", "public_preview_url": url_for("location_detail_page", location_id=rid) if rid else ""})
    elif resource_key == "delivery-pricing":
        common.update(
            {
                "fields": _management_delivery_pricing_fields(),
                "submit_url": "/api/admin/delivery-pricing" if creating else f"/api/admin/delivery-pricing/{rid}",
                "submit_method": "POST" if creating else "PATCH",
                "submit_label": "Add Delivery Tier" if creating else "Save Delivery Tier",
            }
        )
    elif resource_key == "hero-media":
        common.update({"fields": _management_hero_fields(), "submit_url": "/api/admin/hero-media" if creating else f"/api/admin/hero-media/{rid}", "submit_method": "POST" if creating else "PATCH", "submit_label": "Add Banner" if creating else "Save Banner"})
    elif resource_key == "customers" and not creating:
        common.update({"title": "Customer Status", "fields": [
            {"name": "full_name", "label": "Customer", "type": "text", "readonly": True},
            {"name": "email", "label": "Email", "type": "email", "readonly": True},
            {"name": "status", "label": "Account Status", "type": "select", "required": True, "options": [{"value": "active", "label": "Active"}, {"value": "disabled", "label": "Disabled"}]},
        ], "submit_url": f"/api/admin/customers/{rid}/status", "submit_method": "PATCH", "submit_label": "Update Customer Status"})
    elif resource_key == "staff":
        if principal.get("role") not in {ROLE_OWNER, ROLE_SUPER_ADMIN}:
            raise AppError("ACCESS_DENIED", "Only Owner or Super Admin may change staff access.", 403)
        common.update({"fields": _management_staff_fields(principal, create=creating), "submit_url": "/api/admin/staff" if creating else f"/api/admin/staff/{rid}", "submit_method": "POST" if creating else "PATCH", "submit_label": "Create Staff" if creating else "Save Staff Access"})
    elif resource_key == "reviews" and not creating:
        common.update({"title": "Moderate Review", "fields": [
            {"name": "rating", "label": "Rating", "type": "integer", "readonly": True},
            {"name": "comment", "label": "Review", "type": "textarea", "readonly": True, "width": "full"},
            {"name": "moderation_state", "label": "Moderation State", "type": "select", "required": True, "options": [{"value": "approved", "label": "Approved"}, {"value": "hidden", "label": "Hidden"}, {"value": "rejected", "label": "Rejected"}]},
            {"name": "moderation_note", "label": "Moderation Note", "type": "textarea", "max_length": 500, "width": "full"},
        ], "submit_url": f"/api/admin/reviews/{rid}/moderation", "submit_method": "PATCH", "submit_label": "Save Moderation"})
    elif resource_key == "payments" and not creating:
        method = record.get("method")
        status = record.get("status")
        action_options: list[dict[str, str]] = []
        if method == "bank_transfer" and status in {"pending", "awaiting_verification"}:
            action_options = [{"value": "verify_bank", "label": "Verify Bank Transfer"}, {"value": "reject_bank", "label": "Reject Bank Transfer"}]
        elif method == "cod" and status == "pending":
            action_options = [{"value": "mark_cod_paid", "label": "Mark COD Paid"}]
        if not action_options:
            raise AppError("INVALID_PAYMENT_TRANSITION", "This payment has no manual action available in its current state.", 409)
        record["action"] = action_options[0]["value"]
        common.update({"title": "Payment Verification", "fields": [
            {"name": "method", "label": "Method", "type": "text", "readonly": True},
            {"name": "status", "label": "Current Status", "type": "status", "readonly": True},
            {"name": "action", "label": "Action", "type": "select", "required": True, "options": action_options},
            {"name": "note", "label": "Verification Note", "type": "textarea", "max_length": 500, "width": "full"},
        ], "submit_url": f"/api/admin/payments/{rid}/action", "submit_method": "POST", "submit_label": "Apply Payment Action"})
    elif resource_key == "bookings" and not creating:
        common.update({"title": "Edit Booking", "fields": [
            {"name": "booking_reference", "label": "Booking", "type": "text", "readonly": True},
            {"name": "status", "label": "Status", "type": "select", "required": True, "options": [{"value": s, "label": s.replace("_", " ").title()} for s in sorted(BOOKING_STATUSES)]},
            {"name": "date", "label": "Date", "type": "date", "required": True},
            {"name": "time", "label": "Time", "type": "time", "required": True},
            {"name": "guests", "label": "Guests", "type": "integer", "required": True, "min": 1, "max": 100},
            {"name": "internal_note", "label": "Internal Note", "type": "textarea", "max_length": 1000, "width": "full"},
        ], "submit_url": f"/api/admin/bookings/{rid}", "submit_method": "PATCH", "submit_label": "Save Booking"})
    elif resource_key == "feedback" and not creating:
        common.update({"title": "Update Feedback", "fields": [
            {"name": "name", "label": "Name", "type": "text", "readonly": True},
            {"name": "email", "label": "Email", "type": "email", "readonly": True},
            {"name": "message", "label": "Message", "type": "textarea", "readonly": True, "width": "full"},
            {"name": "status", "label": "Status", "type": "text", "required": True, "max_length": 40},
            {"name": "internal_note", "label": "Internal Note", "type": "textarea", "max_length": 500, "width": "full"},
        ], "submit_url": f"/api/admin/feedback/{rid}", "submit_method": "PATCH", "submit_label": "Save Feedback"})
    elif resource_key == "orders" and not creating:
        mode = record.get("service_mode")
        current = record.get("order_status")
        possible = sorted(ORDER_TRANSITIONS.get(mode, {}).get(current, set()))
        if principal.get("role") == ROLE_CHEF:
            possible = [x for x in possible if x in CHEF_ALLOWED_STATUS_TARGETS]
        if not _management_has_permission(principal, "cancel_orders"):
            possible = [x for x in possible if x != "cancelled"]
        common.update({"title": "Update Order Status", "fields": [
            {"name": "order_reference", "label": "Order", "type": "text", "readonly": True},
            {"name": "order_status", "label": "Current Status", "type": "status", "readonly": True},
            {"name": "status", "label": "Next Status", "type": "select", "required": True, "empty_label": "Choose next status", "options": [{"value": s, "label": s.replace("_", " ").title()} for s in possible]},
            {"name": "note", "label": "Status / Cancellation Note", "type": "textarea", "max_length": 500, "width": "full"},
        ], "submit_url": f"/api/admin/orders/{rid}/status", "submit_method": "POST", "submit_label": "Update Order"})
    else:
        raise AppError("METHOD_NOT_ALLOWED", "This management record is read-only in the browser console.", 405)
    return common


def management_dashboard_bundle(principal: dict[str, Any]) -> dict[str, Any]:
    links: list[dict[str, str]] = []
    preferred = ["products", "categories", "deals", "hero-media", "locations", "delivery-pricing", "orders", "customers", "staff", "reviews", "payments", "bookings", "feedback"]
    for key in preferred:
        spec = MANAGEMENT_RESOURCE_SPECS[key]
        if _management_has_permission(principal, spec.get("view_permission")):
            links.append({"label": spec["title"], "description": spec["description"], "url": _management_url(key)})
    return {
        "role_label": principal.get("role", "staff").replace("_", " ").title(),
        "staff_display_name": principal.get("name", ""),
        "business_day_label": business_now().strftime("%A, %d %B %Y"),
        "last_updated_label": business_now().strftime("%H:%M"),
        "quick_links": links,
    }


@app.get("/admin/manage")
@staff_required
def manage_home():
    principal = current_principal()
    rows = []
    for key, spec in MANAGEMENT_RESOURCE_SPECS.items():
        if not _management_has_permission(principal, spec.get("view_permission")):
            continue
        rows.append({
            "id": key,
            "name": spec["title"],
            "description": spec["description"],
            "actions": [{"label": "Open", "url": _management_url(key), "method": "GET", "style": "primary"}],
        })
    resource = {
        "key": "management", "title": "Management Center", "description": "Backend-authorized administration areas available to your current staff account.",
        "authorized": True, "icon": "records", "columns": [{"key": "name", "label": "Area"}, {"key": "description", "label": "Purpose", "truncate": 110}],
        "rows": rows, "result_count": len(rows), "search": {"enabled": False}, "filters": [], "sort_options": [], "create_action": {}, "id_key": "id", "display_key": "name", "row_actions_key": "actions",
    }
    return render_page("manage_list.html", resource=resource)


@app.get("/admin/manage/<resource_key>")
@staff_required
def manage_resource_list(resource_key: str):
    resource_key = str(resource_key or "").strip().lower()
    return render_page("manage_list.html", resource=_management_list_payload(resource_key))


@app.get("/admin/manage/<resource_key>/new")
@staff_required
def manage_resource_create_page(resource_key: str):
    resource_key = str(resource_key or "").strip().lower()
    return render_page("manage_form.html", form_config=_management_form_payload(resource_key))


@app.get("/admin/manage/<resource_key>/<record_id>/edit")
@staff_required
def manage_resource_edit_page(resource_key: str, record_id: str):
    resource_key = str(resource_key or "").strip().lower()
    return render_page("manage_form.html", form_config=_management_form_payload(resource_key, record_id))


@app.get("/admin/manage/products/<product_id>/options")
@staff_required
def manage_product_options_page(product_id: str):
    _management_spec("products", write=True)
    product = get_document("products", validate_id(product_id, "product_id"), required=True, code="PRODUCT_NOT_FOUND")
    return render_page(
        "product_options.html",
        product=product,
        option_groups=get_product_option_groups(product["id"]),
        options_submit_url=f"/api/admin/products/{product['id']}/options",
        products_list_url=_management_url("products"),
    )


@app.get("/admin/manage/products/<product_id>/gallery")
@staff_required
def manage_product_gallery_page(product_id: str):
    _management_spec("products", write=True)
    product = get_document("products", validate_id(product_id, "product_id"), required=True, code="PRODUCT_NOT_FOUND")
    return render_page(
        "product_gallery_admin.html",
        product=product,
        primary_image_url=str(product.get("image_url") or product.get("image") or ""),
        gallery_images=product_gallery_extra_images(product),
        gallery_submit_url=f"/api/admin/products/{product['id']}/gallery",
        gallery_upload_url=f"/api/admin/products/{product['id']}/gallery/upload",
        products_list_url=_management_url("products"),
        product_edit_url=_management_url("products", product["id"], mode="edit"),
        max_gallery_images=MAX_PRODUCT_GALLERY_IMAGES,
    )


# =============================================================================
# Dashboard routes
# =============================================================================

@app.get("/owner/dashboard")
@owner_required
def owner_dashboard():
    p = current_principal()
    return render_page("dashboard.html", metrics=dashboard_metrics(p), dashboard=management_dashboard_bundle(p))



def _public_content_media_value(value: Any, field: str) -> str:
    """Validate an About-page image as a same-site path or an http(s) URL."""
    media = validate_text(value, field, max_len=2048)
    if not media:
        return ""
    if media.startswith("/") and not media.startswith("//"):
        return media
    return validate_url(media, field)


def _about_editor_values(about: dict[str, Any]) -> dict[str, str]:
    """Flatten the structured public About page into safe owner-editor form values."""
    data = about if isinstance(about, dict) else {}
    hero = data.get("hero") if isinstance(data.get("hero"), dict) else {}
    sections = data.get("sections") if isinstance(data.get("sections"), list) else []

    story = next((x for x in sections if isinstance(x, dict) and x.get("type") == "image_text"), {})
    values_section = next(
        (x for x in sections if isinstance(x, dict) and x.get("type") in {"cards", "feature_grid"}),
        {},
    )
    stats_section = next((x for x in sections if isinstance(x, dict) and x.get("type") == "stats"), {})
    quote_section = next((x for x in sections if isinstance(x, dict) and x.get("type") == "quote"), {})

    value_items = values_section.get("items") if isinstance(values_section.get("items"), list) else []
    stat_items = stats_section.get("items") if isinstance(stats_section.get("items"), list) else []
    final_cta = data.get("final_cta") if isinstance(data.get("final_cta"), dict) else {}

    def item(items: list[Any], index: int) -> dict[str, Any]:
        if 0 <= index < len(items) and isinstance(items[index], dict):
            return items[index]
        return {}

    default_story = (
        f"{BUSINESS_NAME} is built around fresh food, careful preparation, and friendly service. "
        "Every order is prepared with the same attention we would want for our own table."
    )
    defaults = {
        "eyebrow": "Our Story",
        "title": f"About {BUSINESS_NAME}",
        "subtitle": "Fresh food, warm service, and a passion for every order.",
        "intro": "From the first order to the last delivery of the day, our goal is simple: make every customer feel welcome and every meal worth coming back for.",
        "hero_image": "/static/images/hero/hero-4.jpg",
        "hero_alt": f"{BUSINESS_NAME} restaurant",
        "story_eyebrow": "Who We Are",
        "story_title": "Made with care. Served with heart.",
        "story_text": default_story,
        "story_image": "/static/images/hero/hero-5.jpg",
        "story_image_alt": "Restaurant food and dining experience",
        "value_section_title": "What matters to us",
        "value_1_title": "Fresh Ingredients",
        "value_1_text": "We focus on quality ingredients and consistent preparation.",
        "value_2_title": "Made with Care",
        "value_2_text": "Every order is checked and prepared with attention to detail.",
        "value_3_title": "Friendly Service",
        "value_3_text": "Delivery, pickup, and dine-in should all feel simple and welcoming.",
        "stat_1_value": "Fresh",
        "stat_1_label": "Ingredients",
        "stat_2_value": "3",
        "stat_2_label": "Service Modes",
        "stat_3_value": "100%",
        "stat_3_label": "Customer Focus",
        "quote": "Good food brings people together. Great service gives them a reason to come back.",
        "quote_source": BUSINESS_NAME,
        "cta_title": "Ready to order?",
        "cta_description": "Explore the menu, choose your favorites, and place your order in a few simple steps.",
    }

    values = dict(defaults)
    values.update(
        {
            "eyebrow": str(data.get("eyebrow") or defaults["eyebrow"]),
            "title": str(data.get("title") or defaults["title"]),
            "subtitle": str(data.get("subtitle") or data.get("description") or defaults["subtitle"]),
            "intro": str(data.get("intro") or defaults["intro"]),
            "hero_image": str(hero.get("image_url") or hero.get("image") or defaults["hero_image"]),
            "hero_alt": str(hero.get("image_alt") or defaults["hero_alt"]),
            "story_eyebrow": str(story.get("eyebrow") or defaults["story_eyebrow"]),
            "story_title": str(story.get("title") or defaults["story_title"]),
            "story_text": str(story.get("text") or defaults["story_text"]),
            "story_image": str(
                ((story.get("image") or {}).get("url") if isinstance(story.get("image"), dict) else "")
                or story.get("image_url")
                or defaults["story_image"]
            ),
            "story_image_alt": str(
                ((story.get("image") or {}).get("alt") if isinstance(story.get("image"), dict) else "")
                or story.get("image_alt")
                or defaults["story_image_alt"]
            ),
            "value_section_title": str(values_section.get("title") or defaults["value_section_title"]),
            "quote": str(quote_section.get("quote") or quote_section.get("text") or defaults["quote"]),
            "quote_source": str(
                quote_section.get("attribution") or quote_section.get("source") or defaults["quote_source"]
            ),
            "cta_title": str(final_cta.get("title") or defaults["cta_title"]),
            "cta_description": str(
                final_cta.get("description") or final_cta.get("text") or defaults["cta_description"]
            ),
        }
    )

    for index in range(3):
        value_item = item(value_items, index)
        stat_item = item(stat_items, index)
        n = index + 1
        values[f"value_{n}_title"] = str(value_item.get("title") or defaults[f"value_{n}_title"])
        values[f"value_{n}_text"] = str(
            value_item.get("description") or value_item.get("text") or defaults[f"value_{n}_text"]
        )
        values[f"stat_{n}_value"] = str(stat_item.get("value") or defaults[f"stat_{n}_value"])
        values[f"stat_{n}_label"] = str(stat_item.get("label") or defaults[f"stat_{n}_label"])

    return values


@app.route("/owner/website-content", methods=["GET", "POST"])
@owner_required
def owner_website_content_page():
    """Owner-only editor for the public About page stored in settings/public_content."""
    current = get_document("settings", "public_content") or {}
    pages = current.get("pages") if isinstance(current.get("pages"), dict) else {}
    about = pages.get("about") if isinstance(pages.get("about"), dict) else {}
    if not about and isinstance(current.get("about"), dict):
        about = current.get("about") or {}

    if request.method == "POST":
        form = request.form
        page_title = validate_text(form.get("title"), "title", required=True, max_len=140)
        page_subtitle = validate_text(form.get("subtitle"), "subtitle", required=True, max_len=320)

        about_page = {
            "eyebrow": validate_text(form.get("eyebrow"), "eyebrow", required=True, max_len=80),
            "title": page_title,
            "subtitle": page_subtitle,
            "intro": validate_text(form.get("intro"), "intro", required=True, max_len=1200),
            "theme": "warm",
            "mode": "default",
            "hero": {
                "variant": "warm",
                "image_url": _public_content_media_value(form.get("hero_image"), "hero_image"),
                "image_alt": validate_text(form.get("hero_alt"), "hero_alt", max_len=180),
                "primary_cta": {"label": "Explore Menu", "endpoint": "menu_page", "style": "primary"},
                "secondary_cta": {
                    "label": "Our Locations",
                    "endpoint": "locations_page",
                    "style": "secondary",
                },
            },
            "sections": [
                {
                    "type": "image_text",
                    "background": "surface",
                    "eyebrow": validate_text(
                        form.get("story_eyebrow"),
                        "story_eyebrow",
                        required=True,
                        max_len=80,
                    ),
                    "title": validate_text(
                        form.get("story_title"),
                        "story_title",
                        required=True,
                        max_len=160,
                    ),
                    "text": validate_text(
                        form.get("story_text"),
                        "story_text",
                        required=True,
                        max_len=2000,
                    ),
                    "image": {
                        "url": _public_content_media_value(form.get("story_image"), "story_image"),
                        "alt": validate_text(
                            form.get("story_image_alt"),
                            "story_image_alt",
                            max_len=180,
                        ),
                        "aspect": "landscape",
                    },
                    "image_position": "right",
                },
                {
                    "type": "feature_grid",
                    "background": "soft",
                    "eyebrow": "Our Values",
                    "title": validate_text(
                        form.get("value_section_title"),
                        "value_section_title",
                        required=True,
                        max_len=160,
                    ),
                    "items": [
                        {
                            "icon": "fresh",
                            "title": validate_text(
                                form.get("value_1_title"),
                                "value_1_title",
                                required=True,
                                max_len=100,
                            ),
                            "description": validate_text(
                                form.get("value_1_text"),
                                "value_1_text",
                                required=True,
                                max_len=420,
                            ),
                        },
                        {
                            "icon": "quality",
                            "title": validate_text(
                                form.get("value_2_title"),
                                "value_2_title",
                                required=True,
                                max_len=100,
                            ),
                            "description": validate_text(
                                form.get("value_2_text"),
                                "value_2_text",
                                required=True,
                                max_len=420,
                            ),
                        },
                        {
                            "icon": "users",
                            "title": validate_text(
                                form.get("value_3_title"),
                                "value_3_title",
                                required=True,
                                max_len=100,
                            ),
                            "description": validate_text(
                                form.get("value_3_text"),
                                "value_3_text",
                                required=True,
                                max_len=420,
                            ),
                        },
                    ],
                },
                {
                    "type": "stats",
                    "background": "surface",
                    "eyebrow": "At a Glance",
                    "title": "What we focus on every day",
                    "items": [
                        {
                            "value": validate_text(
                                form.get("stat_1_value"),
                                "stat_1_value",
                                required=True,
                                max_len=40,
                            ),
                            "label": validate_text(
                                form.get("stat_1_label"),
                                "stat_1_label",
                                required=True,
                                max_len=80,
                            ),
                        },
                        {
                            "value": validate_text(
                                form.get("stat_2_value"),
                                "stat_2_value",
                                required=True,
                                max_len=40,
                            ),
                            "label": validate_text(
                                form.get("stat_2_label"),
                                "stat_2_label",
                                required=True,
                                max_len=80,
                            ),
                        },
                        {
                            "value": validate_text(
                                form.get("stat_3_value"),
                                "stat_3_value",
                                required=True,
                                max_len=40,
                            ),
                            "label": validate_text(
                                form.get("stat_3_label"),
                                "stat_3_label",
                                required=True,
                                max_len=80,
                            ),
                        },
                    ],
                },
                {
                    "type": "quote",
                    "background": "soft",
                    "quote": validate_text(
                        form.get("quote"),
                        "quote",
                        required=True,
                        max_len=700,
                    ),
                    "attribution": validate_text(
                        form.get("quote_source"),
                        "quote_source",
                        required=True,
                        max_len=120,
                    ),
                },
            ],
            "final_cta": {
                "eyebrow": "Order Online",
                "title": validate_text(
                    form.get("cta_title"),
                    "cta_title",
                    required=True,
                    max_len=140,
                ),
                "description": validate_text(
                    form.get("cta_description"),
                    "cta_description",
                    required=True,
                    max_len=500,
                ),
                "primary": {"label": "Order Now", "endpoint": "menu_page", "style": "primary"},
                "secondary": {
                    "label": "Find a Location",
                    "endpoint": "locations_page",
                    "style": "secondary",
                },
            },
            "seo": {
                "title": page_title,
                "description": page_subtitle,
            },
        }

        safe_about = sanitize_settings_object(about_page)
        existing_pages = dict(pages)
        existing_pages["about"] = safe_about

        require_db().collection("settings").document("public_content").set(
            {
                "pages": existing_pages,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "updated_by": current_principal()["uid"],
            },
            merge=True,
        )
        audit_log(
            "website_content.about.updated",
            "settings",
            "public_content",
            {"page": "about"},
        )
        return redirect(url_for("owner_website_content_page", saved="1"))

    return render_page(
        "website_content.html",
        form_values=_about_editor_values(about),
        saved=request.args.get("saved") == "1",
    )


@app.get("/super-admin/dashboard")
@staff_required
@permission_required("view_dashboard")
def super_admin_dashboard():
    p = current_principal()
    if p["role"] != ROLE_SUPER_ADMIN:
        raise AppError("ACCESS_DENIED", "Super Admin access is required.", 403)
    return render_page("dashboard.html", metrics=dashboard_metrics(p), dashboard=management_dashboard_bundle(p))


@app.get("/admin/dashboard")
@staff_required
@permission_required("view_dashboard")
def admin_dashboard():
    p = current_principal()
    if p["role"] != ROLE_ADMIN:
        raise AppError("ACCESS_DENIED", "Admin access is required.", 403)
    return render_page("dashboard.html", metrics=dashboard_metrics(p), dashboard=management_dashboard_bundle(p))


@app.get("/chef/dashboard")
@staff_required
@permission_required("kitchen_access")
def chef_dashboard():
    p = current_principal()
    if p["role"] != ROLE_CHEF:
        raise AppError("ACCESS_DENIED", "Chef access is required.", 403)
    return render_page("dashboard.html", metrics=dashboard_metrics(p), dashboard=management_dashboard_bundle(p))


@app.get("/api/admin/dashboard")
@staff_required
@permission_required("view_dashboard")
def dashboard_api():
    return api_success(dashboard_metrics(current_principal()))


# =============================================================================
# Staff order management
# =============================================================================

def staff_order_query_from_request() -> Any:
    filters: list[tuple[str, str, Any]] = []
    status = request.args.get("status", "").strip()
    if status:
        valid_statuses = set(DELIVERY_STATUSES + PICKUP_STATUSES + DINE_IN_STATUSES)
        if status not in valid_statuses:
            raise AppError("VALIDATION_ERROR", "Invalid order status filter.", 422)
        filters.append(("order_status", "==", status))
    outlet_id = request.args.get("outlet_id", "").strip()
    if outlet_id:
        filters.append(("outlet_id", "==", validate_id(outlet_id, "outlet_id")))
    service_mode = request.args.get("service_mode", "").strip()
    if service_mode:
        filters.append(("service_mode", "==", validate_service_mode(service_mode)))
    payment_status = request.args.get("payment_status", "").strip()
    if payment_status:
        if payment_status not in PAYMENT_STATUSES:
            raise AppError("VALIDATION_ERROR", "Invalid payment status filter.", 422)
        filters.append(("payment_status", "==", payment_status))
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if date_from:
        day = parse_date_value(date_from, "date_from", True)
        assert day
        filters.append(("placed_at", ">=", business_day_bounds(day)[0]))
    if date_to:
        day = parse_date_value(date_to, "date_to", True)
        assert day
        filters.append(("placed_at", "<", business_day_bounds(day + timedelta(days=1))[0]))
    return firestore_query("orders", filters)


@app.get("/api/admin/orders")
@staff_required
@permission_required("view_orders")
def staff_orders_api():
    p = current_principal()
    sort_key = request.args.get("sort", "placed_at")
    sort_field = SAFE_ORDER_SORT_FIELDS.get(sort_key)
    if not sort_field:
        raise AppError("INVALID_SORT", "Unsupported order sort field.", 422)
    direction = request.args.get("direction", "desc").lower()
    if direction not in {"asc", "desc"}:
        raise AppError("INVALID_SORT", "Sort direction must be asc or desc.", 422)
    q = staff_order_query_from_request()
    items, cursor = paginate_query(
        "orders", q, order_field=sort_field, direction=direction, limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"orders": [staff_order_view(x, p) for x in items], "next_cursor": cursor})


@app.get("/api/admin/orders/<order_id>")
@staff_required
@permission_required("view_orders")
def staff_order_detail_api(order_id: str):
    p = current_principal()
    order = get_document("orders", validate_id(order_id, "order_id"), required=True, code="ORDER_NOT_FOUND")
    data = {
        "order": staff_order_view(order, p),
        "history": order_history(order["id"]),
    }
    if p["role"] != ROLE_CHEF and (p["role"] == ROLE_OWNER or "view_payments" in set(p.get("permissions", []))):
        data["payment"] = get_document("payments", order.get("payment_id", "")) if order.get("payment_id") else None
    return api_success(data)


CHEF_ALLOWED_STATUS_TARGETS = {"preparing", "packing", "ready_for_pickup", "sent_to_kitchen", "ready"}
STATUS_TIMESTAMP_FIELDS = {
    "confirmed": "confirmed_at",
    "preparing": "preparing_at",
    "packing": "packing_at",
    "rider_assigned": "rider_assigned_at",
    "out_for_delivery": "out_for_delivery_at",
    "delivered": "delivered_at",
    "ready_for_pickup": "ready_for_pickup_at",
    "picked_up": "picked_up_at",
    "sent_to_kitchen": "sent_to_kitchen_at",
    "ready": "ready_at",
    "serving": "serving_at",
    "served": "served_at",
    "completed": "completed_at",
    "cancelled": "cancelled_at",
}


def staff_has_permission(p: dict[str, Any], permission: str) -> bool:
    return p.get("role") == ROLE_OWNER or permission in set(p.get("permissions", []))


@app.post("/api/admin/orders/<order_id>/status")
@limiter.limit("180 per hour")
@staff_required
@permission_required("update_orders")
def update_order_status_api(order_id: str):
    p = current_principal()
    order_id = validate_id(order_id, "order_id")
    data = payload_data()
    new_status = str(data.get("status") or "").strip().lower()
    note = validate_text(data.get("note"), "note", max_len=500)
    db = require_db()
    order_ref = db.collection("orders").document(order_id)
    history_ref = db.collection("order_status_history").document()
    transaction = db.transaction()

    @firestore.transactional
    def status_txn(txn):
        snap = order_ref.get(transaction=txn)
        if not snap.exists:
            raise AppError("ORDER_NOT_FOUND", "Order was not found.", 404)
        order = snap.to_dict() or {}
        mode = order.get("service_mode")
        current = order.get("order_status")
        if mode not in ORDER_TRANSITIONS:
            raise AppError("INVALID_ORDER_STATE", "Order service mode is invalid.", 409)
        if new_status not in ORDER_TRANSITIONS[mode].get(current, set()):
            raise AppError(
                "INVALID_STATUS_TRANSITION",
                f"Cannot move order from {current} to {new_status}.",
                409,
            )
        if new_status == "cancelled" and not staff_has_permission(p, "cancel_orders"):
            raise AppError("PERMISSION_DENIED", "You do not have permission to cancel orders.", 403)
        if p["role"] == ROLE_CHEF and new_status not in CHEF_ALLOWED_STATUS_TARGETS:
            raise AppError("PERMISSION_DENIED", "Chef role cannot perform this order status transition.", 403)
        if order.get("payment_method") in {"easypaisa", "bank_transfer"} and order.get("payment_status") != "paid" and new_status != "cancelled":
            raise AppError("PAYMENT_REQUIRED", "Online/manual transfer payment must be verified before kitchen processing.", 409)

        update: dict[str, Any] = {"order_status": new_status, "updated_at": firestore.SERVER_TIMESTAMP}
        timestamp_field = STATUS_TIMESTAMP_FIELDS.get(new_status)
        if timestamp_field:
            update[timestamp_field] = firestore.SERVER_TIMESTAMP
        if new_status == "cancelled":
            update["cancellation_reason"] = note
        txn.update(order_ref, update)
        txn.set(
            history_ref,
            {
                "order_id": order_id,
                "previous_status": current,
                "new_status": new_status,
                "actor_uid": p["uid"],
                "actor_role": p["role"],
                "note": note,
                "created_at": firestore.SERVER_TIMESTAMP,
            },
        )
        return {"customer_uid": order.get("customer_uid"), "order_reference": order.get("order_reference"), "previous": current}

    result = status_txn(transaction)
    audit_log(
        "order.status_changed" if new_status != "cancelled" else "order.cancelled",
        "order",
        order_id,
        {"previous_status": result["previous"], "new_status": new_status},
    )
    create_notification(
        recipient_uid=result.get("customer_uid"),
        notification_type="transactional",
        title="Order update",
        message=f"Order {result.get('order_reference')} is now {new_status.replace('_', ' ')}.",
        related_order_id=order_id,
    )
    return api_success(get_document("orders", order_id, required=True))


@app.post("/api/admin/orders/<order_id>/internal-note")
@staff_required
@permission_required("update_orders")
def add_internal_order_note_api(order_id: str):
    p = current_principal()
    if p["role"] == ROLE_CHEF:
        raise AppError("PERMISSION_DENIED", "Chef accounts cannot add administrative internal notes.", 403)
    order_id = validate_id(order_id, "order_id")
    get_document("orders", order_id, required=True, code="ORDER_NOT_FOUND")
    data = payload_data()
    note = validate_text(data.get("note"), "note", required=True, min_len=2, max_len=1000)
    entry = {"note": note, "actor_uid": p["uid"], "actor_role": p["role"], "created_at": utcnow()}
    require_db().collection("orders").document(order_id).update(
        {"internal_notes": firestore.ArrayUnion([entry]), "updated_at": firestore.SERVER_TIMESTAMP}
    )
    audit_log("order.internal_note_added", "order", order_id)
    return api_success({"message": "Internal note added."})


# =============================================================================
# Management validation helpers
# =============================================================================

def validate_outlet_configuration(data: dict[str, Any]) -> tuple[list[str], dict[str, bool]]:
    outlet_ids = safe_list_of_ids(data.get("outlet_ids", []), "outlet_ids", 100)
    availability_raw = data.get("availability_by_outlet") or {}
    if not isinstance(availability_raw, dict):
        raise AppError("VALIDATION_ERROR", "availability_by_outlet must be an object.", 422)
    availability: dict[str, bool] = {}
    if len(availability_raw) > 100:
        raise AppError("VALIDATION_ERROR", "Too many outlet availability entries.", 422)
    for key, value in availability_raw.items():
        oid = validate_id(key, "outlet_id")
        get_document("locations", oid, required=True, code="LOCATION_NOT_FOUND")
        availability[oid] = validate_bool(value)
    for oid in outlet_ids:
        get_document("locations", oid, required=True, code="LOCATION_NOT_FOUND")
    return outlet_ids, availability


def validate_keywords(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, list) or len(value) > 40:
        raise AppError("VALIDATION_ERROR", "search_keywords must be a list with at most 40 entries.", 422)
    result = []
    for keyword in value:
        normalized = normalize_search(validate_text(keyword, "search_keyword", required=True, max_len=80))
        if normalized:
            result.append(normalized)
    return list(dict.fromkeys(result))


def ensure_unique_slug(collection: str, slug: str, current_id: Optional[str] = None) -> None:
    existing = first_document(collection, [("slug", "==", slug)])
    if existing and existing.get("id") != current_id:
        raise AppError("DUPLICATE_SLUG", f"Slug '{slug}' is already in use.", 409)


# =============================================================================
# Category management
# =============================================================================

def category_payload(data: dict[str, Any], *, existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if existing is None or "name" in data:
        out["name"] = validate_text(data.get("name"), "name", required=True, min_len=2, max_len=100)
        out["name_search"] = normalize_search(out["name"])
    if existing is None or "slug" in data or "name" in data:
        slug = validate_slug(data.get("slug"), "slug") if data.get("slug") else slugify(out.get("name") or existing.get("name", ""))
        ensure_unique_slug("categories", slug, existing.get("id") if existing else None)
        out["slug"] = slug
    if "description" in data or existing is None:
        out["description"] = validate_text(data.get("description"), "description", max_len=1000)
    if "image_url" in data or existing is None:
        out["image_url"] = validate_url(data.get("image_url"), "image_url", False)
    if "icon" in data or existing is None:
        out["icon"] = validate_text(data.get("icon"), "icon", max_len=200)
    if "display_order" in data or existing is None:
        out["display_order"] = validate_int(data.get("display_order", 0), "display_order", 0, 10000)
    if "active" in data or existing is None:
        out["active"] = validate_bool(data.get("active"), True)
    return out


@app.get("/api/admin/categories")
@staff_required
@permission_required("view_categories")
def admin_categories_list():
    q = firestore_query("categories")
    items, cursor = paginate_query(
        "categories", q, order_field="display_order", direction="asc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"categories": items, "next_cursor": cursor})


@app.post("/api/admin/categories")
@staff_required
@permission_required("manage_categories")
def admin_category_create():
    data = category_payload(payload_data())
    ref = require_db().collection("categories").document()
    ref.set(set_server_timestamps(data, creating=True))
    audit_log("category.created", "category", ref.id, {"slug": data.get("slug")})
    return api_success(get_document("categories", ref.id, required=True), 201)


@app.patch("/api/admin/categories/<category_id>")
@staff_required
@permission_required("manage_categories")
def admin_category_update(category_id: str):
    category = get_document("categories", validate_id(category_id, "category_id"), required=True, code="CATEGORY_NOT_FOUND")
    updates = category_payload(payload_data(), existing=category)
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    require_db().collection("categories").document(category["id"]).set(updates, merge=True)
    audit_log("category.updated", "category", category["id"], {"fields": sorted(updates.keys())})
    return api_success(get_document("categories", category["id"], required=True))


@app.post("/api/admin/categories/<category_id>/deactivate")
@staff_required
@permission_required("manage_categories")
def admin_category_deactivate(category_id: str):
    category = get_document("categories", validate_id(category_id, "category_id"), required=True, code="CATEGORY_NOT_FOUND")
    require_db().collection("categories").document(category["id"]).set(
        {"active": False, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("category.deactivated", "category", category["id"])
    return api_success({"id": category["id"], "active": False})


@app.post("/api/admin/categories/<category_id>/reactivate")
@staff_required
@permission_required("manage_categories")
def admin_category_reactivate(category_id: str):
    category = get_document("categories", validate_id(category_id, "category_id"), required=True, code="CATEGORY_NOT_FOUND")
    require_db().collection("categories").document(category["id"]).set(
        {"active": True, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("category.reactivated", "category", category["id"])
    return api_success({"id": category["id"], "active": True})


@app.post("/api/admin/categories/reorder")
@staff_required
@permission_required("manage_categories")
def admin_categories_reorder():
    data = payload_data()
    ids = safe_list_of_ids(data.get("category_ids"), "category_ids", 100)
    if not ids:
        raise AppError("VALIDATION_ERROR", "category_ids is required.", 422)
    db = require_db()
    batch = db.batch()
    for order, cid in enumerate(ids):
        snap = db.collection("categories").document(cid).get()
        if not snap.exists:
            raise AppError("CATEGORY_NOT_FOUND", f"Category {cid} was not found.", 404)
        batch.set(db.collection("categories").document(cid), {"display_order": order, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    batch.commit()
    audit_log("category.reordered", "category", "multiple", {"count": len(ids)})
    return api_success({"reordered": len(ids)})


# =============================================================================
# Product management
# =============================================================================

def validate_product_gallery_images(
    value: Any,
    *,
    primary_image_url: str = "",
) -> list[str]:
    """Validate and normalize ordered additional product image URLs.

    The existing image_url field stays authoritative as the menu/cover image.
    gallery_images contains only additional images and preserves display order.
    """
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise AppError(
            "VALIDATION_ERROR",
            "gallery_images must be a list of image URLs.",
            422,
            {"field": "gallery_images"},
        )
    if len(value) > MAX_PRODUCT_GALLERY_IMAGES:
        raise AppError(
            "VALIDATION_ERROR",
            f"gallery_images may contain at most {MAX_PRODUCT_GALLERY_IMAGES} images.",
            422,
            {"field": "gallery_images"},
        )

    primary = str(primary_image_url or "").strip()
    normalized: list[str] = []
    for index, item in enumerate(value):
        # Accept a simple URL string. A small object with a `url` key is also
        # accepted so the future admin UI can evolve without changing the API.
        raw_url = item.get("url") if isinstance(item, dict) else item
        url = validate_url(raw_url, f"gallery_images[{index}]", False)
        if not url or url == primary or url in normalized:
            continue
        normalized.append(url)

    return normalized


def product_gallery_extra_images(product: dict[str, Any]) -> list[str]:
    """Return validated additional gallery URLs stored on a product."""
    stored = product.get("gallery_images", [])
    primary = str(product.get("image_url") or product.get("image") or "")
    try:
        return validate_product_gallery_images(stored, primary_image_url=primary)
    except AppError:
        # Legacy or manually-edited data should not break the public product
        # page. Invalid entries are ignored here; admin writes remain strict.
        cleaned: list[str] = []
        if isinstance(stored, list):
            for item in stored[:MAX_PRODUCT_GALLERY_IMAGES]:
                raw_url = item.get("url") if isinstance(item, dict) else item
                try:
                    url = validate_url(raw_url, "gallery_image", False)
                except AppError:
                    continue
                if url and url != primary and url not in cleaned:
                    cleaned.append(url)
        return cleaned


def product_gallery_images(product: dict[str, Any]) -> list[str]:
    """Return the public gallery: primary image first, then ordered extras."""
    images: list[str] = []
    primary = str(product.get("image_url") or product.get("image") or "").strip()
    if primary:
        images.append(primary)
    for url in product_gallery_extra_images(product):
        if url not in images:
            images.append(url)
    return images


def product_payload(data: dict[str, Any], *, existing: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], bool]:
    out: dict[str, Any] = {}
    pricing_changed = False
    if existing is None or "name" in data:
        out["name"] = validate_text(data.get("name"), "name", required=True, min_len=2, max_len=160)
        out["name_search"] = normalize_search(out["name"])
    if existing is None or "slug" in data or "name" in data:
        slug = validate_slug(data.get("slug"), "slug") if data.get("slug") else slugify(out.get("name") or existing.get("name", ""))
        ensure_unique_slug("products", slug, existing.get("id") if existing else None)
        out["slug"] = slug
    if existing is None or "description" in data:
        out["description"] = validate_text(data.get("description"), "description", max_len=3000)
    if existing is None or "category_id" in data:
        cid = validate_id(data.get("category_id"), "category_id")
        category = get_document("categories", cid, required=True, code="CATEGORY_NOT_FOUND")
        if not category.get("active", False):
            raise AppError("CATEGORY_INACTIVE", "Selected category is inactive.", 409)
        out["category_id"] = cid
    if existing is None or "base_price" in data:
        new_price_minor = money_to_minor(data.get("base_price"), "base_price")
        out["base_price_minor"] = new_price_minor
        pricing_changed = existing is None or int((existing or {}).get("base_price_minor", -1)) != new_price_minor
    if existing is None or "image_url" in data:
        out["image_url"] = validate_url(data.get("image_url"), "image_url", False)
        out["image"] = out["image_url"]
    if existing is None:
        out["gallery_images"] = []
    for field, default in (
        ("active", True),
        ("available", True),
        ("featured", False),
        ("popular", False),
        ("spicy", False),
        ("vegetarian", False),
    ):
        if existing is None or field in data:
            out[field] = validate_bool(data.get(field), default)
    if existing is None or "display_order" in data:
        out["display_order"] = validate_int(data.get("display_order", 0), "display_order", 0, 100000)
    if existing is None or "name" in data or "category_id" in data or "search_keywords" in data:
        supplied = validate_keywords(
            data.get("search_keywords", (existing or {}).get("search_keywords", []))
        )
        product_name = normalize_search(str(out.get("name") or (existing or {}).get("name", "")))
        category_id_for_search = out.get("category_id") or (existing or {}).get("category_id")
        category_name = ""
        if category_id_for_search:
            category_for_search = get_document("categories", str(category_id_for_search))
            category_name = normalize_search(str((category_for_search or {}).get("name", "")))
        generated = [product_name, category_name]
        generated += [token for token in re.split(r"[^a-z0-9]+", product_name) if len(token) >= 2]
        generated += [token for token in re.split(r"[^a-z0-9]+", category_name) if len(token) >= 2]
        out["search_keywords"] = list(dict.fromkeys(x for x in generated + supplied if x))[:40]
    if existing is None or "outlet_ids" in data or "availability_by_outlet" in data:
        merged = dict(existing or {})
        merged.update(data)
        outlet_ids, availability = validate_outlet_configuration(merged)
        out["outlet_ids"] = outlet_ids
        out["availability_by_outlet"] = availability
    return out, pricing_changed


@app.get("/api/admin/products")
@staff_required
@permission_required("view_products")
def admin_products_list():
    q = firestore_query("products")
    sort_key = request.args.get("sort", "updated_at")
    sort_field = SAFE_PRODUCT_SORT_FIELDS.get(sort_key)
    if not sort_field:
        raise AppError("INVALID_SORT", "Unsupported product sort field.", 422)
    direction = request.args.get("direction", "desc")
    if direction not in {"asc", "desc"}:
        raise AppError("INVALID_SORT", "Sort direction must be asc or desc.", 422)
    items, cursor = paginate_query(
        "products", q, order_field=sort_field, direction=direction, limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"products": items, "next_cursor": cursor})


@app.post("/api/admin/products")
@staff_required
@permission_required("manage_products")
def admin_product_create():
    data, _ = product_payload(payload_data())
    uploads = _validated_product_image_uploads(
        PRODUCT_IMAGE_UPLOAD_FIELD,
        max_files=MAX_PRODUCT_UPLOAD_FILES,
    )
    data["pricing_version"] = 1
    ref = require_db().collection("products").document()
    saved_paths: list[Path] = []
    if uploads:
        image_urls, saved_paths = _save_product_image_uploads(ref.id, uploads)
        data["image_url"] = image_urls[0]
        data["image"] = image_urls[0]
        data["gallery_images"] = image_urls[1:]
    try:
        ref.set(set_server_timestamps(data, creating=True))
    except Exception:
        _cleanup_saved_product_images(saved_paths)
        raise
    audit_log("product.created", "product", ref.id, {"base_price_minor": data.get("base_price_minor")})
    return api_success(get_document("products", ref.id, required=True), 201)


@app.patch("/api/admin/products/<product_id>")
@staff_required
@permission_required("manage_products")
def admin_product_update(product_id: str):
    product = get_document("products", validate_id(product_id, "product_id"), required=True, code="PRODUCT_NOT_FOUND")
    updates, pricing_changed = product_payload(payload_data(), existing=product)
    uploads = _validated_product_image_uploads(
        PRODUCT_IMAGE_UPLOAD_FIELD,
        max_files=MAX_PRODUCT_UPLOAD_FILES,
    )
    saved_paths: list[Path] = []
    if uploads:
        image_urls, saved_paths = _save_product_image_uploads(product["id"], uploads)
        updates["image_url"] = image_urls[0]
        updates["image"] = image_urls[0]
        updates["gallery_images"] = image_urls[1:]
    if pricing_changed:
        updates["pricing_version"] = int(product.get("pricing_version", 1)) + 1
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    try:
        require_db().collection("products").document(product["id"]).set(updates, merge=True)
    except Exception:
        _cleanup_saved_product_images(saved_paths)
        raise
    audit_log(
        "product.updated",
        "product",
        product["id"],
        {"fields": sorted(updates.keys()), "old_price_minor": product.get("base_price_minor"), "new_price_minor": updates.get("base_price_minor")},
    )
    return api_success(get_document("products", product["id"], required=True))


@app.post("/api/admin/products/<product_id>/deactivate")
@staff_required
@permission_required("manage_products")
def admin_product_deactivate(product_id: str):
    product = get_document("products", validate_id(product_id, "product_id"), required=True, code="PRODUCT_NOT_FOUND")
    require_db().collection("products").document(product["id"]).set(
        {"active": False, "available": False, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("product.deactivated", "product", product["id"])
    return api_success({"id": product["id"], "active": False})


@app.post("/api/admin/products/<product_id>/reactivate")
@staff_required
@permission_required("manage_products")
def admin_product_reactivate(product_id: str):
    product = get_document("products", validate_id(product_id, "product_id"), required=True, code="PRODUCT_NOT_FOUND")
    require_db().collection("products").document(product["id"]).set(
        {"active": True, "available": True, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("product.reactivated", "product", product["id"])
    return api_success({"id": product["id"], "active": True})


@app.get("/api/admin/products/<product_id>/gallery")
@staff_required
@permission_required("view_products")
def admin_product_gallery_get(product_id: str):
    product = get_document(
        "products",
        validate_id(product_id, "product_id"),
        required=True,
        code="PRODUCT_NOT_FOUND",
    )
    return api_success(
        {
            "product_id": product["id"],
            "primary_image_url": str(product.get("image_url") or product.get("image") or ""),
            "gallery_images": product_gallery_extra_images(product),
            "all_images": product_gallery_images(product),
            "max_gallery_images": MAX_PRODUCT_GALLERY_IMAGES,
        }
    )


@app.post("/api/admin/products/<product_id>/gallery/upload")
@staff_required
@permission_required("manage_products")
def admin_product_gallery_upload(product_id: str):
    product = get_document(
        "products",
        validate_id(product_id, "product_id"),
        required=True,
        code="PRODUCT_NOT_FOUND",
    )
    existing_images = product_gallery_extra_images(product)
    remaining = MAX_PRODUCT_GALLERY_IMAGES - len(existing_images)
    uploads = _validated_product_image_uploads(
        PRODUCT_GALLERY_UPLOAD_FIELD,
        max_files=remaining,
    )
    if not uploads:
        raise AppError(
            "IMAGE_REQUIRED",
            "Select at least one product image to upload.",
            422,
            {"field": PRODUCT_GALLERY_UPLOAD_FIELD},
        )

    image_urls, saved_paths = _save_product_image_uploads(
        product["id"],
        uploads,
        start_position=len(existing_images) + 2,
    )
    db = require_db()
    product_ref = db.collection("products").document(product["id"])
    transaction = db.transaction()

    @firestore.transactional
    def append_gallery(txn):
        snap = product_ref.get(transaction=txn)
        if not snap.exists:
            raise AppError("PRODUCT_NOT_FOUND", "Product was not found.", 404)
        current = snap.to_dict() or {}
        current["id"] = snap.id
        gallery = product_gallery_extra_images(current)
        if len(gallery) + len(image_urls) > MAX_PRODUCT_GALLERY_IMAGES:
            raise AppError(
                "TOO_MANY_IMAGES",
                f"This product may contain at most {MAX_PRODUCT_GALLERY_IMAGES} additional gallery images.",
                409,
            )
        merged = gallery + [url for url in image_urls if url not in gallery]
        txn.set(
            product_ref,
            {"gallery_images": merged, "updated_at": firestore.SERVER_TIMESTAMP},
            merge=True,
        )
        return merged

    try:
        merged_images = append_gallery(transaction)
    except Exception:
        _cleanup_saved_product_images(saved_paths)
        raise

    audit_log(
        "product.gallery_images_uploaded",
        "product",
        product["id"],
        {"uploaded_count": len(image_urls), "gallery_image_count": len(merged_images)},
    )
    updated = get_document("products", product["id"], required=True, code="PRODUCT_NOT_FOUND")
    return api_success(
        {
            "product_id": product["id"],
            "primary_image_url": str(updated.get("image_url") or updated.get("image") or ""),
            "gallery_images": product_gallery_extra_images(updated),
            "all_images": product_gallery_images(updated),
            "max_gallery_images": MAX_PRODUCT_GALLERY_IMAGES,
        },
        201,
    )


@app.put("/api/admin/products/<product_id>/gallery")
@staff_required
@permission_required("manage_products")
def admin_product_gallery_replace(product_id: str):
    product = get_document(
        "products",
        validate_id(product_id, "product_id"),
        required=True,
        code="PRODUCT_NOT_FOUND",
    )
    data = payload_data()
    primary = str(product.get("image_url") or product.get("image") or "")
    images = validate_product_gallery_images(
        data.get("gallery_images"),
        primary_image_url=primary,
    )
    require_db().collection("products").document(product["id"]).set(
        {
            "gallery_images": images,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    audit_log(
        "product.gallery_replaced",
        "product",
        product["id"],
        {"gallery_image_count": len(images)},
    )
    updated = get_document("products", product["id"], required=True, code="PRODUCT_NOT_FOUND")
    return api_success(
        {
            "product_id": product["id"],
            "primary_image_url": str(updated.get("image_url") or updated.get("image") or ""),
            "gallery_images": product_gallery_extra_images(updated),
            "all_images": product_gallery_images(updated),
            "max_gallery_images": MAX_PRODUCT_GALLERY_IMAGES,
        }
    )


def _is_size_option_group(group_key: str, name: str) -> bool:
    key = normalize_search(group_key).replace(" ", "_")
    label = normalize_search(name)
    return key in {"size", "sizes", "pizza_size", "pizza_sizes"} or label in {
        "size",
        "sizes",
        "pizza size",
        "pizza sizes",
    }


def validate_option_groups_payload(groups: Any, product_id: str) -> list[dict[str, Any]]:
    if not isinstance(groups, list) or len(groups) > 50:
        raise AppError("VALIDATION_ERROR", "option_groups must be a list with at most 50 groups.", 422)

    product = get_document("products", validate_id(product_id, "product_id"), required=True, code="PRODUCT_NOT_FOUND")
    base_price_minor = int(product.get("base_price_minor") or 0)

    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_size_group = False
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise AppError("VALIDATION_ERROR", "Each option group must be an object.", 422)

        name = validate_text(group.get("name"), "option_group.name", required=True, max_len=100)
        group_key = validate_id(group.get("group_key") or slugify(name).replace("-", "_"), "group_key")
        if group_key in seen_keys:
            raise AppError("VALIDATION_ERROR", f"Duplicate option group key: {group_key}.", 422)
        seen_keys.add(group_key)

        size_group = _is_size_option_group(group_key, name)
        if size_group:
            if seen_size_group:
                raise AppError(
                    "VALIDATION_ERROR",
                    "Only one pizza Size option group is allowed per product.",
                    422,
                )
            seen_size_group = True

        required = True if size_group else validate_bool(group.get("required"), False)
        min_sel = 1 if size_group else validate_int(group.get("min_selections", 1 if required else 0), "min_selections", 0, 30)
        max_sel = 1 if size_group else validate_int(group.get("max_selections", 1), "max_selections", 0, 30)
        if max_sel < min_sel:
            raise AppError("VALIDATION_ERROR", "max_selections cannot be less than min_selections.", 422)

        options_raw = group.get("options") or []
        if not isinstance(options_raw, list) or len(options_raw) > 100:
            raise AppError("VALIDATION_ERROR", "Each option group may contain at most 100 options.", 422)

        options: list[dict[str, Any]] = []
        seen_option_ids: set[str] = set()
        for option in options_raw:
            if not isinstance(option, dict):
                raise AppError("VALIDATION_ERROR", "Each product option must be an object.", 422)

            option_name = validate_text(option.get("name"), "option.name", required=True, max_len=100)
            option_id = validate_id(option.get("id") or slugify(option_name).replace("-", "_"), "option_id")
            if option_id in seen_option_ids:
                raise AppError("VALIDATION_ERROR", f"Duplicate option id {option_id} in group {group_key}.", 422)
            seen_option_ids.add(option_id)

            final_price_minor: Optional[int] = None
            if option.get("final_price_minor") not in (None, ""):
                final_price_minor = validate_int(
                    option.get("final_price_minor"),
                    "final_price_minor",
                    0,
                    100_000_000,
                )
            elif option.get("final_price") not in (None, ""):
                final_price_minor = money_to_minor(option.get("final_price"), "final_price")

            if final_price_minor is not None:
                if final_price_minor < base_price_minor:
                    raise AppError(
                        "VALIDATION_ERROR",
                        f"{option_name} final price cannot be lower than the product base price.",
                        422,
                    )
                price_delta_minor = final_price_minor - base_price_minor
            else:
                price_delta_minor = money_to_minor(option.get("price_delta", "0"), "price_delta")
                if size_group:
                    final_price_minor = base_price_minor + price_delta_minor

            option_doc = {
                "id": option_id,
                "name": option_name,
                "price_delta_minor": price_delta_minor,
                "active": validate_bool(option.get("active"), True),
                "display_order": validate_int(option.get("display_order", len(options)), "option.display_order", 0, 10000),
            }
            if size_group:
                option_doc["final_price_minor"] = int(final_price_minor if final_price_minor is not None else base_price_minor)
            options.append(option_doc)

        if required and not options:
            raise AppError("VALIDATION_ERROR", f"Required option group {group_key} must contain options.", 422)

        result.append(
            {
                "product_id": product_id,
                "group_key": group_key,
                "name": name,
                "required": required,
                "min_selections": min_sel,
                "max_selections": max_sel,
                "active": validate_bool(group.get("active"), True),
                "display_order": validate_int(group.get("display_order", index), "display_order", 0, 10000),
                "pricing_mode": "final_size_price" if size_group else "price_delta",
                "options": options,
            }
        )
    return result


@app.put("/api/admin/products/<product_id>/options")
@staff_required
@permission_required("manage_products")
def admin_product_options_replace(product_id: str):
    product = get_document("products", validate_id(product_id, "product_id"), required=True, code="PRODUCT_NOT_FOUND")
    groups = validate_option_groups_payload(payload_data().get("option_groups"), product["id"])
    db = require_db()
    existing = list(firestore_query("product_options", [("product_id", "==", product["id"])]).limit(100).stream())
    if len(existing) >= 100:
        raise AppError("OPTION_LIMIT_EXCEEDED", "Too many existing option groups to safely replace in one request.", 409)
    batch = db.batch()
    for snap in existing:
        batch.delete(snap.reference)
    for group in groups:
        ref = db.collection("product_options").document()
        batch.set(ref, set_server_timestamps(group, creating=True))
    batch.set(
        db.collection("products").document(product["id"]),
        {
            "pricing_version": int(product.get("pricing_version", 1)) + 1,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    batch.commit()
    audit_log("product.options_replaced", "product", product["id"], {"group_count": len(groups)})
    return api_success({"product_id": product["id"], "option_groups": get_product_option_groups(product["id"])})


# =============================================================================
# Deal management
# =============================================================================

def validate_included_items(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise AppError("VALIDATION_ERROR", "included_items must be a list with at most 50 items.", 422)
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise AppError("VALIDATION_ERROR", "Each included deal item must be an object.", 422)
        product_id = validate_id(item.get("product_id"), "product_id")
        product = get_product(product_id)
        result.append(
            {
                "product_id": product_id,
                "product_name": product.get("name", ""),
                "quantity": validate_int(item.get("quantity", 1), "quantity", 1, 20),
                "allow_customization": validate_bool(item.get("allow_customization"), False),
            }
        )
    return result


def deal_payload(data: dict[str, Any], *, existing: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], bool]:
    out: dict[str, Any] = {}
    pricing_changed = False
    if existing is None or "name" in data:
        out["name"] = validate_text(data.get("name"), "name", required=True, min_len=2, max_len=160)
        out["name_search"] = normalize_search(out["name"])
    if existing is None or "description" in data:
        out["description"] = validate_text(data.get("description"), "description", max_len=3000)
    if existing is None or "price" in data:
        new_price_minor = money_to_minor(data.get("price"), "price")
        out["price_minor"] = new_price_minor
        pricing_changed = existing is None or int((existing or {}).get("price_minor", -1)) != new_price_minor
    if existing is None or "image_url" in data:
        out["image_url"] = validate_url(data.get("image_url"), "image_url", False)
        out["image"] = out["image_url"]
    if existing is None or "included_items" in data:
        out["included_items"] = validate_included_items(data.get("included_items", []))
        if existing is not None:
            pricing_changed = True
    for field, default in (("active", True), ("featured", False)):
        if existing is None or field in data:
            out[field] = validate_bool(data.get(field), default)
    if existing is None or "display_order" in data:
        out["display_order"] = validate_int(data.get("display_order", 0), "display_order", 0, 100000)
    if existing is None or "start_at" in data:
        out["start_at"] = parse_iso_datetime(data.get("start_at"), "start_at", False)
    if existing is None or "end_at" in data:
        out["end_at"] = parse_iso_datetime(data.get("end_at"), "end_at", False)
    start = out.get("start_at", existing.get("start_at") if existing else None)
    end = out.get("end_at", existing.get("end_at") if existing else None)
    if start and end and end <= start:
        raise AppError("VALIDATION_ERROR", "Deal end_at must be later than start_at.", 422)
    if existing is None or "outlet_ids" in data or "availability_by_outlet" in data:
        merged = dict(existing or {})
        merged.update(data)
        outlet_ids, availability = validate_outlet_configuration(merged)
        out["outlet_ids"] = outlet_ids
        out["availability_by_outlet"] = availability
    return out, pricing_changed


@app.get("/api/admin/deals")
@staff_required
@permission_required("view_deals")
def admin_deals_list():
    q = firestore_query("deals")
    items, cursor = paginate_query(
        "deals", q, order_field="updated_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"deals": items, "next_cursor": cursor})


@app.post("/api/admin/deals")
@staff_required
@permission_required("manage_deals")
def admin_deal_create():
    data, _ = deal_payload(payload_data())
    data["pricing_version"] = 1
    ref = require_db().collection("deals").document()
    ref.set(set_server_timestamps(data, creating=True))
    audit_log("deal.created", "deal", ref.id, {"price_minor": data.get("price_minor")})
    return api_success(get_document("deals", ref.id, required=True), 201)


@app.patch("/api/admin/deals/<deal_id>")
@staff_required
@permission_required("manage_deals")
def admin_deal_update(deal_id: str):
    deal = get_document("deals", validate_id(deal_id, "deal_id"), required=True, code="DEAL_NOT_FOUND")
    updates, pricing_changed = deal_payload(payload_data(), existing=deal)
    if pricing_changed:
        updates["pricing_version"] = int(deal.get("pricing_version", 1)) + 1
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    require_db().collection("deals").document(deal["id"]).set(updates, merge=True)
    audit_log("deal.updated", "deal", deal["id"], {"fields": sorted(updates.keys())})
    return api_success(get_document("deals", deal["id"], required=True))


@app.post("/api/admin/deals/<deal_id>/deactivate")
@staff_required
@permission_required("manage_deals")
def admin_deal_deactivate(deal_id: str):
    deal = get_document("deals", validate_id(deal_id, "deal_id"), required=True, code="DEAL_NOT_FOUND")
    require_db().collection("deals").document(deal["id"]).set(
        {"active": False, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("deal.deactivated", "deal", deal["id"])
    return api_success({"id": deal["id"], "active": False})


@app.post("/api/admin/deals/<deal_id>/reactivate")
@staff_required
@permission_required("manage_deals")
def admin_deal_reactivate(deal_id: str):
    deal = get_document("deals", validate_id(deal_id, "deal_id"), required=True, code="DEAL_NOT_FOUND")
    require_db().collection("deals").document(deal["id"]).set(
        {"active": True, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("deal.reactivated", "deal", deal["id"])
    return api_success({"id": deal["id"], "active": True})


# =============================================================================
# Location management
# =============================================================================

# =============================================================================
# Delivery pricing management
# =============================================================================

def _decimal_distance(value: Any, field: str, *, minimum: Decimal = Decimal("0")) -> float:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AppError("VALIDATION_ERROR", f"{field} must be a valid number.", 422, {"field": field})
    if number < minimum or number > Decimal("500"):
        raise AppError("VALIDATION_ERROR", f"{field} must be between {minimum} and 500.", 422, {"field": field})
    return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _assert_delivery_tier_no_overlap(
    candidate: dict[str, Any],
    *,
    ignore_id: Optional[str] = None,
) -> None:
    if not candidate.get("active", True):
        return
    outlet_id = str(candidate.get("outlet_id") or "")
    candidate_min = float(candidate["min_distance_km"])
    candidate_max = float(candidate["max_distance_km"])

    existing = _scan_collection_with_filters(
        "delivery_pricing_tiers",
        [("active", "==", True)],
        scan_limit=250,
    )
    for tier in existing:
        if ignore_id and tier.get("id") == ignore_id:
            continue
        if str(tier.get("outlet_id") or "") != outlet_id:
            continue
        try:
            tier_min = float(tier.get("min_distance_km", 0))
            tier_max = float(tier.get("max_distance_km", 0))
        except (TypeError, ValueError):
            continue
        # Adjacent boundaries are allowed: 0-5 and 5-10.
        if max(candidate_min, tier_min) < min(candidate_max, tier_max):
            scope = "global pricing" if not outlet_id else "this outlet"
            raise AppError(
                "DELIVERY_TIER_OVERLAP",
                f"This distance range overlaps another active delivery tier for {scope}.",
                409,
                {"conflicting_tier_id": tier.get("id")},
            )


def delivery_pricing_payload(
    data: dict[str, Any],
    *,
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    if existing is None or "label" in data:
        out["label"] = validate_text(data.get("label"), "label", required=True, max_len=120)

    if existing is None or "outlet_id" in data:
        outlet_id = str(data.get("outlet_id") or "").strip()
        if outlet_id:
            outlet_id = validate_id(outlet_id, "outlet_id")
            outlet = get_document("locations", outlet_id, required=True, code="LOCATION_NOT_FOUND")
            if not outlet.get("delivery_enabled", False):
                raise AppError("DELIVERY_NOT_ENABLED", "Delivery is not enabled for the selected outlet.", 409)
        out["outlet_id"] = outlet_id

    if existing is None or "min_distance_km" in data:
        out["min_distance_km"] = _decimal_distance(data.get("min_distance_km"), "min_distance_km")

    if existing is None or "max_distance_km" in data:
        out["max_distance_km"] = _decimal_distance(
            data.get("max_distance_km"),
            "max_distance_km",
            minimum=Decimal("0.01"),
        )

    minimum_distance = float(out.get("min_distance_km", (existing or {}).get("min_distance_km", 0)))
    maximum_distance = float(out.get("max_distance_km", (existing or {}).get("max_distance_km", 0)))
    if maximum_distance <= minimum_distance:
        raise AppError(
            "VALIDATION_ERROR",
            "max_distance_km must be greater than min_distance_km.",
            422,
        )

    if existing is None or "fee" in data:
        out["fee_minor"] = money_to_minor(data.get("fee"), "fee")

    if existing is None or "active" in data:
        out["active"] = validate_bool(data.get("active"), True)

    if existing is None or "display_order" in data:
        out["display_order"] = validate_int(data.get("display_order", 0), "display_order", 0, 100000)

    return out


@app.get("/api/admin/delivery-pricing")
@staff_required
@permission_required("view_locations")
def admin_delivery_pricing_list():
    items = ordered_documents(
        "delivery_pricing_tiers",
        (),
        order_field="display_order",
        direction="asc",
        limit=min(query_limit(), 200),
    )
    return api_success({"delivery_pricing": items})


@app.post("/api/admin/delivery-pricing")
@staff_required
@permission_required("manage_locations")
def admin_delivery_pricing_create():
    data = delivery_pricing_payload(payload_data())
    _assert_delivery_tier_no_overlap(data)
    ref = require_db().collection("delivery_pricing_tiers").document()
    ref.set(set_server_timestamps(data, creating=True))
    audit_log(
        "delivery_pricing.created",
        "delivery_pricing_tier",
        ref.id,
        {
            "outlet_id": data.get("outlet_id"),
            "min_distance_km": data.get("min_distance_km"),
            "max_distance_km": data.get("max_distance_km"),
            "fee_minor": data.get("fee_minor"),
        },
    )
    return api_success(get_document("delivery_pricing_tiers", ref.id, required=True), 201)


@app.patch("/api/admin/delivery-pricing/<tier_id>")
@staff_required
@permission_required("manage_locations")
def admin_delivery_pricing_update(tier_id: str):
    tier_id = validate_id(tier_id, "tier_id")
    existing = get_document("delivery_pricing_tiers", tier_id, required=True, code="DELIVERY_TIER_NOT_FOUND")
    updates = delivery_pricing_payload(payload_data(), existing=existing)
    merged = {**existing, **updates}
    _assert_delivery_tier_no_overlap(merged, ignore_id=tier_id)
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    require_db().collection("delivery_pricing_tiers").document(tier_id).set(updates, merge=True)
    audit_log("delivery_pricing.updated", "delivery_pricing_tier", tier_id, {"fields": sorted(updates.keys())})
    return api_success(get_document("delivery_pricing_tiers", tier_id, required=True))


@app.post("/api/admin/delivery-pricing/<tier_id>/deactivate")
@staff_required
@permission_required("manage_locations")
def admin_delivery_pricing_deactivate(tier_id: str):
    tier_id = validate_id(tier_id, "tier_id")
    get_document("delivery_pricing_tiers", tier_id, required=True, code="DELIVERY_TIER_NOT_FOUND")
    require_db().collection("delivery_pricing_tiers").document(tier_id).set(
        {"active": False, "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    audit_log("delivery_pricing.deactivated", "delivery_pricing_tier", tier_id)
    return api_success({"id": tier_id, "active": False})


@app.post("/api/admin/delivery-pricing/<tier_id>/reactivate")
@staff_required
@permission_required("manage_locations")
def admin_delivery_pricing_reactivate(tier_id: str):
    tier_id = validate_id(tier_id, "tier_id")
    existing = get_document("delivery_pricing_tiers", tier_id, required=True, code="DELIVERY_TIER_NOT_FOUND")
    merged = {**existing, "active": True}
    _assert_delivery_tier_no_overlap(merged, ignore_id=tier_id)
    require_db().collection("delivery_pricing_tiers").document(tier_id).set(
        {"active": True, "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )
    audit_log("delivery_pricing.reactivated", "delivery_pricing_tier", tier_id)
    return api_success({"id": tier_id, "active": True})


def validate_opening_hours(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if not isinstance(value, dict):
        raise AppError("VALIDATION_ERROR", "opening_hours must be an object.", 422)
    if len(json.dumps(value)) > 6000:
        raise AppError("VALIDATION_ERROR", "opening_hours is too large.", 422)
    safe: dict[str, Any] = {}
    for day_name, schedule in value.items():
        key = validate_text(day_name, "opening_hours.day", required=True, max_len=20).lower()
        if isinstance(schedule, str):
            safe[key] = validate_text(schedule, "opening_hours.schedule", max_len=100)
        elif isinstance(schedule, dict):
            safe[key] = {
                "open": validate_text(schedule.get("open"), "opening_hours.open", max_len=10),
                "close": validate_text(schedule.get("close"), "opening_hours.close", max_len=10),
                "closed": validate_bool(schedule.get("closed"), False),
            }
        else:
            raise AppError("VALIDATION_ERROR", "Invalid opening_hours schedule.", 422)
    return safe


def location_payload(data: dict[str, Any], *, existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fields = (("name", 120, True), ("address", 400, True), ("city", 100, True), ("area", 120, False))
    for field, max_len, required in fields:
        if existing is None or field in data:
            out[field] = validate_text(data.get(field), field, required=required, max_len=max_len)
    if existing is None or "phone" in data:
        out["phone"] = validate_phone(data.get("phone"), False)
    if existing is None or "latitude" in data:
        out["latitude"] = validate_latitude(data.get("latitude"), False)
    if existing is None or "longitude" in data:
        out["longitude"] = validate_longitude(data.get("longitude"), False)
    if existing is None or "opening_hours" in data:
        out["opening_hours"] = validate_opening_hours(data.get("opening_hours"))
    for field, default in (
        ("delivery_enabled", True),
        ("pickup_enabled", True),
        ("dine_in_enabled", True),
        ("active", True),
    ):
        if existing is None or field in data:
            out[field] = validate_bool(data.get(field), default)
    if existing is None or "delivery_radius_km" in data:
        raw = data.get("delivery_radius_km")
        if raw is None or raw == "":
            out["delivery_radius_km"] = None
        else:
            try:
                radius = Decimal(str(raw))
            except InvalidOperation:
                raise AppError("VALIDATION_ERROR", "Invalid delivery_radius_km.", 422)
            if radius <= 0 or radius > 500:
                raise AppError("VALIDATION_ERROR", "delivery_radius_km must be between 0 and 500.", 422)
            out["delivery_radius_km"] = float(radius)
    if existing is None or "delivery_areas" in data:
        areas = data.get("delivery_areas") or []
        if not isinstance(areas, list) or len(areas) > 200:
            raise AppError("VALIDATION_ERROR", "delivery_areas must be a list with at most 200 items.", 422)
        out["delivery_areas"] = list(
            dict.fromkeys(normalize_search(validate_text(x, "delivery_area", required=True, max_len=120)) for x in areas)
        )
    if existing is None or "display_order" in data:
        out["display_order"] = validate_int(data.get("display_order", 0), "display_order", 0, 100000)
    if existing is None or "image_url" in data:
        out["image_url"] = validate_url(data.get("image_url"), "image_url", False)
    return out


@app.get("/api/admin/locations")
@staff_required
@permission_required("view_locations")
def admin_locations_list():
    q = firestore_query("locations")
    items, cursor = paginate_query(
        "locations", q, order_field="display_order", direction="asc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"locations": items, "next_cursor": cursor})


@app.post("/api/admin/locations")
@staff_required
@permission_required("manage_locations")
def admin_location_create():
    data = location_payload(payload_data())
    ref = require_db().collection("locations").document()
    ref.set(set_server_timestamps(data, creating=True))
    audit_log("location.created", "location", ref.id)
    return api_success(get_document("locations", ref.id, required=True), 201)


@app.patch("/api/admin/locations/<location_id>")
@staff_required
@permission_required("manage_locations")
def admin_location_update(location_id: str):
    location = get_document("locations", validate_id(location_id, "location_id"), required=True, code="LOCATION_NOT_FOUND")
    updates = location_payload(payload_data(), existing=location)
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    require_db().collection("locations").document(location["id"]).set(updates, merge=True)
    audit_log("location.updated", "location", location["id"], {"fields": sorted(updates.keys())})
    return api_success(get_document("locations", location["id"], required=True))


@app.post("/api/admin/locations/<location_id>/deactivate")
@staff_required
@permission_required("manage_locations")
def admin_location_deactivate(location_id: str):
    location = get_document("locations", validate_id(location_id, "location_id"), required=True, code="LOCATION_NOT_FOUND")
    require_db().collection("locations").document(location["id"]).set(
        {"active": False, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("location.deactivated", "location", location["id"])
    return api_success({"id": location["id"], "active": False})


@app.post("/api/admin/locations/<location_id>/reactivate")
@staff_required
@permission_required("manage_locations")
def admin_location_reactivate(location_id: str):
    location = get_document("locations", validate_id(location_id, "location_id"), required=True, code="LOCATION_NOT_FOUND")
    require_db().collection("locations").document(location["id"]).set(
        {"active": True, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("location.reactivated", "location", location["id"])
    return api_success({"id": location["id"], "active": True})


# =============================================================================
# Hero media management (image + video metadata only)
# =============================================================================

def hero_payload(data: dict[str, Any], *, existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if existing is None or "media_type" in data:
        media_type = str(data.get("media_type") or "").strip().lower()
        if media_type not in HERO_MEDIA_TYPES:
            raise AppError("VALIDATION_ERROR", "media_type must be image or video.", 422)
        out["media_type"] = media_type
    media_type = out.get("media_type", existing.get("media_type") if existing else None)
    if existing is None or "media_url" in data:
        out["media_url"] = validate_url(data.get("media_url"), "media_url", True)
    if existing is None or "mobile_media_url" in data:
        out["mobile_media_url"] = validate_url(data.get("mobile_media_url"), "mobile_media_url", False)
    if existing is None or "poster_url" in data:
        out["poster_url"] = validate_url(data.get("poster_url"), "poster_url", False)
    poster = out.get("poster_url", existing.get("poster_url") if existing else "")
    if media_type == "video" and not poster:
        raise AppError("VALIDATION_ERROR", "Video hero items require a poster_url fallback.", 422)
    # Backend metadata tells the future frontend to use browser-safe hero video behavior.
    out["autoplay_muted"] = media_type == "video"
    out["playsinline"] = media_type == "video"
    out["image_fallback_url"] = poster if media_type == "video" else (out.get("media_url") or (existing or {}).get("media_url", ""))
    for field, max_len in (("title", 180), ("subtitle", 500), ("cta_text", 100)):
        if existing is None or field in data:
            out[field] = validate_text(data.get(field), field, max_len=max_len)
    if existing is None or "cta_url" in data:
        cta = validate_text(data.get("cta_url"), "cta_url", max_len=2048)
        if cta and not cta.startswith("/"):
            cta = validate_url(cta, "cta_url", False)
        out["cta_url"] = cta
    if existing is None or "display_order" in data:
        out["display_order"] = validate_int(data.get("display_order", 0), "display_order", 0, 100000)
    if existing is None or "active" in data:
        out["active"] = validate_bool(data.get("active"), True)
    if existing is None or "start_at" in data:
        out["start_at"] = parse_iso_datetime(data.get("start_at"), "start_at", False)
    if existing is None or "end_at" in data:
        out["end_at"] = parse_iso_datetime(data.get("end_at"), "end_at", False)
    start = out.get("start_at", existing.get("start_at") if existing else None)
    end = out.get("end_at", existing.get("end_at") if existing else None)
    if start and end and end <= start:
        raise AppError("VALIDATION_ERROR", "Hero end_at must be later than start_at.", 422)
    return out


@app.get("/api/admin/hero-media")
@staff_required
@permission_required("manage_hero_media")
def admin_hero_list():
    q = firestore_query("hero_media")
    items, cursor = paginate_query(
        "hero_media", q, order_field="display_order", direction="asc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"hero_media": items, "next_cursor": cursor})


@app.post("/api/admin/hero-media")
@staff_required
@permission_required("manage_hero_media")
def admin_hero_create():
    data = hero_payload(payload_data())
    ref = require_db().collection("hero_media").document()
    ref.set(set_server_timestamps(data, creating=True))
    audit_log("hero.created", "hero_media", ref.id, {"media_type": data.get("media_type")})
    return api_success(get_document("hero_media", ref.id, required=True), 201)


@app.patch("/api/admin/hero-media/<hero_id>")
@staff_required
@permission_required("manage_hero_media")
def admin_hero_update(hero_id: str):
    hero = get_document("hero_media", validate_id(hero_id, "hero_id"), required=True, code="HERO_NOT_FOUND")
    updates = hero_payload(payload_data(), existing=hero)
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    require_db().collection("hero_media").document(hero["id"]).set(updates, merge=True)
    audit_log("hero.updated", "hero_media", hero["id"], {"fields": sorted(updates.keys())})
    return api_success(get_document("hero_media", hero["id"], required=True))


@app.post("/api/admin/hero-media/<hero_id>/deactivate")
@staff_required
@permission_required("manage_hero_media")
def admin_hero_deactivate(hero_id: str):
    hero = get_document("hero_media", validate_id(hero_id, "hero_id"), required=True, code="HERO_NOT_FOUND")
    require_db().collection("hero_media").document(hero["id"]).set(
        {"active": False, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("hero.deactivated", "hero_media", hero["id"])
    return api_success({"id": hero["id"], "active": False})


@app.post("/api/admin/hero-media/<hero_id>/reactivate")
@staff_required
@permission_required("manage_hero_media")
def admin_hero_reactivate(hero_id: str):
    hero = get_document("hero_media", validate_id(hero_id, "hero_id"), required=True, code="HERO_NOT_FOUND")
    require_db().collection("hero_media").document(hero["id"]).set(
        {"active": True, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("hero.reactivated", "hero_media", hero["id"])
    return api_success({"id": hero["id"], "active": True})


@app.post("/api/admin/hero-media/reorder")
@staff_required
@permission_required("manage_hero_media")
def admin_hero_reorder():
    ids = safe_list_of_ids(payload_data().get("hero_ids"), "hero_ids", 100)
    if not ids:
        raise AppError("VALIDATION_ERROR", "hero_ids is required.", 422)
    db = require_db()
    batch = db.batch()
    for order, hid in enumerate(ids):
        snap = db.collection("hero_media").document(hid).get()
        if not snap.exists:
            raise AppError("HERO_NOT_FOUND", f"Hero item {hid} was not found.", 404)
        batch.set(snap.reference, {"display_order": order, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    batch.commit()
    audit_log("hero.reordered", "hero_media", "multiple", {"count": len(ids)})
    return api_success({"reordered": len(ids)})


# =============================================================================
# Public payment-method configuration
# =============================================================================

@app.get("/api/payment-methods")
@login_required
def payment_methods_api():
    settings = get_document("settings", "payment_methods") or {}
    bank = settings.get("bank_transfer") if isinstance(settings.get("bank_transfer"), dict) else {}
    bank_destination_configured = bool(
        str(bank.get("account_number") or "").strip()
        or str(bank.get("iban") or "").strip()
    )
    bank_enabled = bool(bank.get("enabled", False) and bank_destination_configured)
    public_bank = {
        "enabled": bank_enabled,
        "coming_soon": not bank_enabled,
        "bank_name": str(bank.get("bank_name") or ""),
        "account_title": str(bank.get("account_title") or ""),
        "account_number": str(bank.get("account_number") or ""),
        "iban": str(bank.get("iban") or ""),
        "instructions": str(bank.get("instructions") or ""),
    }

    real_easypaisa_enabled = bool(
        EASYPAISA_ENABLED
        and EASYPAISA_ENDPOINT
        and EASYPAISA_MERCHANT_ID
        and EASYPAISA_ADAPTER
    )
    demo_enabled = demo_easypaisa_enabled()

    return api_success(
        {
            "cod": {"enabled": bool(settings.get("cod_enabled", True))},
            "easypaisa": {
                "enabled": bool(demo_enabled or real_easypaisa_enabled),
                "mode": "demo" if demo_enabled else ("live" if real_easypaisa_enabled else "unavailable"),
                "flow": "wallet_request",
                "requires_customer_mobile": True,
                "redirect_after_order": True,
                # No merchant destination number is exposed in redirect mode.
                "display_number": "",
                "account_title": "",
            },
            "bank_transfer": public_bank,
        }
    )


# =============================================================================
# Customer management
# =============================================================================

@app.get("/api/admin/customers")
@staff_required
@permission_required("view_customers")
def admin_customers_list():
    filters: list[tuple[str, str, Any]] = [("role", "==", ROLE_CUSTOMER)]
    status = request.args.get("status", "").strip()
    if status:
        if status not in ACCOUNT_STATUSES:
            raise AppError("VALIDATION_ERROR", "Invalid customer status.", 422)
        filters.append(("status", "==", status))
    q = firestore_query("users", filters)
    items, cursor = paginate_query(
        "users", q, order_field="created_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"customers": items, "next_cursor": cursor})


@app.get("/api/admin/customers/<uid>")
@staff_required
@permission_required("view_customers")
def admin_customer_detail(uid: str):
    uid = validate_id(uid, "uid")
    customer = get_document("users", uid, required=True, code="CUSTOMER_NOT_FOUND")
    if customer.get("role") != ROLE_CUSTOMER:
        raise AppError("CUSTOMER_NOT_FOUND", "Customer was not found.", 404)
    orders, cursor = customer_order_history_page(uid, limit=20)
    return api_success({"customer": customer, "recent_orders": orders, "next_cursor": cursor})


@app.patch("/api/admin/customers/<uid>/status")
@staff_required
@permission_required("manage_customers")
def admin_customer_status(uid: str):
    uid = validate_id(uid, "uid")
    customer = get_document("users", uid, required=True, code="CUSTOMER_NOT_FOUND")
    if customer.get("role") != ROLE_CUSTOMER:
        raise AppError("CUSTOMER_NOT_FOUND", "Customer was not found.", 404)
    status = str(payload_data().get("status") or "").strip().lower()
    if status not in ACCOUNT_STATUSES:
        raise AppError("VALIDATION_ERROR", "Status must be active or disabled.", 422)
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        auth.update_user(uid, disabled=(status == "disabled"), app=FIREBASE_APP)
        if status == "disabled":
            auth.revoke_refresh_tokens(uid, app=FIREBASE_APP)
    except Exception as exc:
        logger.exception("Firebase customer status update failed.")
        raise AppError("AUTH_UPDATE_FAILED", "Customer authentication status could not be updated.", 502) from exc
    require_db().collection("users").document(uid).set(
        {"status": status, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("customer.status_changed", "user", uid, {"status": status})
    return api_success({"uid": uid, "status": status})


# =============================================================================
# Staff / permissions management
# =============================================================================

def allowed_staff_roles_for_actor(actor_role: str) -> set[str]:
    if actor_role == ROLE_OWNER:
        return {ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_CHEF}
    if actor_role == ROLE_SUPER_ADMIN:
        return {ROLE_ADMIN, ROLE_CHEF}
    return set()


def validate_staff_permissions(role: str, raw: Any, actor: dict[str, Any]) -> list[str]:
    if raw is None or raw == "":
        requested = set(DEFAULT_ROLE_PERMISSIONS.get(role, set()))
    else:
        if not isinstance(raw, list) or len(raw) > len(PERMISSIONS):
            raise AppError("INVALID_STAFF_PERMISSION", "permissions must be a list of valid permission names.", 422)
        requested = {str(x) for x in raw}
        unknown = requested - PERMISSIONS
        if unknown:
            raise AppError("INVALID_STAFF_PERMISSION", f"Unknown permission(s): {', '.join(sorted(unknown))}.", 422)
    if role == ROLE_CHEF:
        chef_allowed = {"view_dashboard", "view_orders", "update_orders", "kitchen_access"}
        requested &= chef_allowed
        requested |= {"view_dashboard", "view_orders", "kitchen_access"}
    if actor["role"] == ROLE_SUPER_ADMIN:
        # Super Admin cannot delegate owner-reserved settings control.
        requested.discard("manage_settings")
    return sorted(requested)


def assert_actor_can_manage_staff(actor: dict[str, Any], target: dict[str, Any], *, allow_self: bool = False) -> None:
    if not allow_self and target.get("uid") == actor.get("uid"):
        raise AppError("SELF_PRIVILEGE_CHANGE_DENIED", "You cannot change your own staff role or permissions.", 403)
    target_role = target.get("role")
    if ROLE_RANK.get(actor.get("role"), -1) <= ROLE_RANK.get(target_role, 999):
        raise AppError("ROLE_ESCALATION_DENIED", "You cannot manage a staff member at or above your role.", 403)


@app.get("/api/admin/permissions")
@staff_required
@permission_required("manage_permissions")
def admin_permissions_list():
    return api_success(
        {
            "permissions": sorted(PERMISSIONS),
            "role_defaults": {role: sorted(perms) for role, perms in DEFAULT_ROLE_PERMISSIONS.items()},
        }
    )


@app.get("/api/admin/staff")
@staff_required
@permission_required("manage_staff")
def admin_staff_list():
    q = firestore_query("staff")
    role = request.args.get("role", "").strip()
    if role:
        if role not in STAFF_ROLES:
            raise AppError("VALIDATION_ERROR", "Invalid staff role.", 422)
        q = q.where(filter=FieldFilter("role", "==", role))
    items, cursor = paginate_query(
        "staff", q, order_field="created_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"staff": items, "next_cursor": cursor})


@app.post("/api/admin/staff")
@limiter.limit("30 per day")
@staff_required
@permission_required("manage_staff")
def admin_staff_create():
    actor = current_principal()
    if actor["role"] not in {ROLE_OWNER, ROLE_SUPER_ADMIN}:
        raise AppError("ROLE_ESCALATION_DENIED", "Only Owner or Super Admin may create staff accounts.", 403)
    data = payload_data()
    role = str(data.get("role") or "").strip().lower()
    if role not in allowed_staff_roles_for_actor(actor["role"]):
        raise AppError("ROLE_ESCALATION_DENIED", "You cannot create this staff role.", 403)
    email = validate_email(data.get("email"), True)
    name = validate_name(data.get("full_name"), "full_name", True)
    permissions = validate_staff_permissions(role, data.get("permissions"), actor)
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        user_record = auth.create_user(email=email, display_name=name, disabled=False, app=FIREBASE_APP)
    except auth.EmailAlreadyExistsError as exc:
        raise AppError("EMAIL_ALREADY_EXISTS", "A Firebase account already exists for this email.", 409) from exc
    except Exception as exc:
        logger.exception("Firebase staff creation failed.")
        raise AppError("STAFF_CREATE_FAILED", "Staff authentication account could not be created.", 502) from exc

    uid = user_record.uid
    db = require_db()
    batch = db.batch()
    batch.set(
        db.collection("users").document(uid),
        {
            "uid": uid,
            "full_name": name,
            "email": email,
            "phone": "",
            "role": role,
            "status": "active",
            "email_verified": bool(user_record.email_verified),
            "auth_provider": "password",
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
    )
    batch.set(
        db.collection("staff").document(uid),
        {
            "uid": uid,
            "full_name": name,
            "email": email,
            "role": role,
            "permissions": permissions,
            "status": "active",
            "created_by": actor["uid"],
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
    )
    batch.commit()

    invite_delivered = False
    try:
        invite_delivered = send_password_reset_email_server_side(email)
    except Exception:
        logger.exception("Staff invite delivery failed after account creation.")
    audit_log("staff.created", "staff", uid, {"role": role, "permissions": permissions, "invite_delivered": invite_delivered})
    return api_success(
        {
            "staff": get_document("staff", uid, required=True),
            "invite_delivered": invite_delivered,
            "message": "Staff account created. No plaintext password was stored or returned.",
        },
        201,
    )


@app.patch("/api/admin/staff/<uid>")
@staff_required
@permission_required("manage_staff")
def admin_staff_update(uid: str):
    actor = current_principal()
    if actor["role"] not in {ROLE_OWNER, ROLE_SUPER_ADMIN}:
        raise AppError("ROLE_ESCALATION_DENIED", "Only Owner or Super Admin may change staff access.", 403)
    uid = validate_id(uid, "uid")
    staff = get_document("staff", uid, required=True, code="STAFF_NOT_FOUND")
    assert_actor_can_manage_staff(actor, staff)
    data = payload_data()
    updates: dict[str, Any] = {}
    role = staff.get("role")
    if "role" in data:
        requested_role = str(data.get("role") or "").strip().lower()
        if requested_role not in allowed_staff_roles_for_actor(actor["role"]):
            raise AppError("ROLE_ESCALATION_DENIED", "You cannot assign this staff role.", 403)
        role = requested_role
        updates["role"] = role
    if "permissions" in data or "role" in data:
        updates["permissions"] = validate_staff_permissions(role, data.get("permissions", staff.get("permissions", [])), actor)
    if "full_name" in data:
        updates["full_name"] = validate_name(data.get("full_name"), "full_name", True)
    if not updates:
        raise AppError("NO_CHANGES", "No permitted staff fields were supplied.", 400)
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    db = require_db()
    batch = db.batch()
    batch.set(db.collection("staff").document(uid), updates, merge=True)
    user_updates = {k: v for k, v in updates.items() if k in {"full_name", "role", "updated_at"}}
    batch.set(db.collection("users").document(uid), user_updates, merge=True)
    batch.commit()
    if "full_name" in updates and FIREBASE_APP is not None:
        try:
            auth.update_user(uid, display_name=updates["full_name"], app=FIREBASE_APP)
        except Exception:
            logger.exception("Non-fatal Firebase display name update failed for staff.")
    audit_log("staff.access_updated", "staff", uid, {"fields": sorted(updates.keys()), "role": role})
    return api_success(get_document("staff", uid, required=True))


@app.patch("/api/admin/staff/<uid>/status")
@staff_required
@permission_required("manage_staff")
def admin_staff_status(uid: str):
    actor = current_principal()
    if actor["role"] not in {ROLE_OWNER, ROLE_SUPER_ADMIN}:
        raise AppError("ROLE_ESCALATION_DENIED", "Only Owner or Super Admin may change staff status.", 403)
    uid = validate_id(uid, "uid")
    staff = get_document("staff", uid, required=True, code="STAFF_NOT_FOUND")
    assert_actor_can_manage_staff(actor, staff)
    status = str(payload_data().get("status") or "").strip().lower()
    if status not in ACCOUNT_STATUSES:
        raise AppError("VALIDATION_ERROR", "Status must be active or disabled.", 422)
    if FIREBASE_APP is None:
        raise ConfigurationError("FIREBASE_UNAVAILABLE", "Firebase Authentication is not configured.")
    try:
        auth.update_user(uid, disabled=(status == "disabled"), app=FIREBASE_APP)
        if status == "disabled":
            auth.revoke_refresh_tokens(uid, app=FIREBASE_APP)
    except Exception as exc:
        logger.exception("Firebase staff status update failed.")
        raise AppError("AUTH_UPDATE_FAILED", "Staff authentication status could not be updated.", 502) from exc
    db = require_db()
    batch = db.batch()
    batch.set(db.collection("staff").document(uid), {"status": status, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    batch.set(db.collection("users").document(uid), {"status": status, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    batch.commit()
    audit_log("staff.status_changed", "staff", uid, {"status": status})
    return api_success({"uid": uid, "status": status})


# =============================================================================
# Review moderation
# =============================================================================

@app.get("/api/admin/reviews")
@staff_required
@permission_required("view_reviews")
def admin_reviews_list():
    filters: list[tuple[str, str, Any]] = []
    state = request.args.get("moderation_state", "").strip()
    if state:
        if state not in REVIEW_MODERATION_STATES:
            raise AppError("VALIDATION_ERROR", "Invalid review moderation state.", 422)
        filters.append(("moderation_state", "==", state))
    product_id = request.args.get("product_id", "").strip()
    if product_id:
        filters.append(("product_id", "==", validate_id(product_id, "product_id")))
    q = firestore_query("reviews", filters)
    items, cursor = paginate_query(
        "reviews", q, order_field="created_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"reviews": items, "next_cursor": cursor})


@app.patch("/api/admin/reviews/<review_id>/moderation")
@staff_required
@permission_required("moderate_reviews")
def admin_review_moderate(review_id: str):
    p = current_principal()
    review = get_document("reviews", validate_id(review_id, "review_id"), required=True, code="REVIEW_NOT_FOUND")
    data = payload_data()
    state = str(data.get("moderation_state") or "").strip().lower()
    if state not in REVIEW_MODERATION_STATES - {"pending"}:
        raise AppError("VALIDATION_ERROR", "moderation_state must be approved, hidden, or rejected.", 422)
    note = validate_text(data.get("moderation_note"), "moderation_note", max_len=500)
    require_db().collection("reviews").document(review["id"]).set(
        {
            "moderation_state": state,
            "moderation_note": note,
            "moderated_by": p["uid"],
            "moderated_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    audit_log("review.moderated", "review", review["id"], {"state": state})
    return api_success(get_document("reviews", review["id"], required=True))


# =============================================================================
# Payment management
# =============================================================================

@app.get("/api/admin/payments")
@staff_required
@permission_required("view_payments")
def admin_payments_list():
    filters: list[tuple[str, str, Any]] = []
    status = request.args.get("status", "").strip()
    if status:
        if status not in PAYMENT_STATUSES:
            raise AppError("VALIDATION_ERROR", "Invalid payment status.", 422)
        filters.append(("status", "==", status))
    method = request.args.get("method", "").strip()
    if method:
        if method not in PAYMENT_METHODS:
            raise AppError("VALIDATION_ERROR", "Invalid payment method.", 422)
        filters.append(("method", "==", method))
    q = firestore_query("payments", filters)
    items, cursor = paginate_query(
        "payments", q, order_field="created_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"payments": items, "next_cursor": cursor})


@app.get("/api/admin/payments/<payment_id>")
@staff_required
@permission_required("view_payments")
def admin_payment_detail(payment_id: str):
    payment = get_document("payments", validate_id(payment_id, "payment_id"), required=True, code="PAYMENT_NOT_FOUND")
    order = get_document("orders", payment.get("order_id", "")) if payment.get("order_id") else None
    events = ordered_documents(
        "payment_events",
        [("payment_id", "==", payment["id"])],
        order_field="created_at",
        direction="desc",
        limit=100,
    )
    return api_success({"payment": payment, "order": order, "events": events})


def finalize_manual_payment(payment_id: str, action: str, principal: dict[str, Any], note: str) -> dict[str, Any]:
    db = require_db()
    payment_ref = db.collection("payments").document(payment_id)
    event_ref = db.collection("payment_events").document()
    transaction = db.transaction()

    @firestore.transactional
    def txn_fn(txn):
        psnap = payment_ref.get(transaction=txn)
        if not psnap.exists:
            raise AppError("PAYMENT_NOT_FOUND", "Payment was not found.", 404)
        payment = psnap.to_dict() or {}
        method = payment.get("method")
        order_ref = db.collection("orders").document(payment.get("order_id"))
        osnap = order_ref.get(transaction=txn)
        if not osnap.exists:
            raise AppError("ORDER_NOT_FOUND", "Linked order was not found.", 404)
        current = payment.get("status")

        if action == "verify_bank":
            if method != "bank_transfer" or current not in {"awaiting_verification", "pending"}:
                raise AppError("INVALID_PAYMENT_TRANSITION", "This bank transfer cannot be verified in its current state.", 409)
            new_status = "paid"
        elif action == "reject_bank":
            if method != "bank_transfer" or current not in {"awaiting_verification", "pending"}:
                raise AppError("INVALID_PAYMENT_TRANSITION", "This bank transfer cannot be rejected in its current state.", 409)
            new_status = "rejected"
        elif action == "mark_cod_paid":
            if method != "cod" or current not in {"pending"}:
                raise AppError("INVALID_PAYMENT_TRANSITION", "This COD payment cannot be marked paid in its current state.", 409)
            new_status = "paid"
        else:
            raise AppError("INVALID_PAYMENT_ACTION", "Unsupported payment action.", 422)

        txn.update(
            payment_ref,
            {
                "status": new_status,
                "verified_by": principal["uid"],
                "verification_note": note,
                "verified_at": firestore.SERVER_TIMESTAMP,
                "paid_at": firestore.SERVER_TIMESTAMP if new_status == "paid" else payment.get("paid_at"),
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )
        txn.update(
            order_ref,
            {
                "payment_status": new_status,
                "operational_hold": None if new_status == "paid" else "payment_rejected",
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
        )
        txn.set(
            event_ref,
            {
                "provider": "manual",
                "payment_id": payment_id,
                "order_id": payment.get("order_id"),
                "action": action,
                "status": new_status,
                "actor_uid": principal["uid"],
                "actor_role": principal["role"],
                "note": note,
                "created_at": firestore.SERVER_TIMESTAMP,
            },
        )
        return {"order_id": payment.get("order_id"), "new_status": new_status}

    return txn_fn(transaction)


@app.post("/api/admin/payments/<payment_id>/action")
@staff_required
@permission_required("verify_payments")
def admin_payment_action(payment_id: str):
    p = current_principal()
    payment_id = validate_id(payment_id, "payment_id")
    data = payload_data()
    action = str(data.get("action") or "").strip().lower()
    note = validate_text(data.get("note"), "note", max_len=500)
    result = finalize_manual_payment(payment_id, action, p, note)
    audit_log("payment.manual_action", "payment", payment_id, {"action": action, "new_status": result["new_status"]})
    order = get_document("orders", result["order_id"])
    if order:
        create_notification(
            recipient_uid=order.get("customer_uid"),
            notification_type="transactional",
            title="Payment update",
            message=f"Payment for order {order.get('order_reference')} is {result['new_status']}.",
            related_order_id=order["id"],
        )
    return api_success(get_document("payments", payment_id, required=True))


# =============================================================================
# Booking management
# =============================================================================

@app.get("/api/admin/bookings")
@staff_required
@permission_required("view_bookings")
def admin_bookings_list():
    filters: list[tuple[str, str, Any]] = []
    status = request.args.get("status", "").strip()
    if status:
        if status not in BOOKING_STATUSES:
            raise AppError("VALIDATION_ERROR", "Invalid booking status.", 422)
        filters.append(("status", "==", status))
    outlet_id = request.args.get("outlet_id", "").strip()
    if outlet_id:
        filters.append(("outlet_id", "==", validate_id(outlet_id, "outlet_id")))
    q = firestore_query("bookings", filters)
    items, cursor = paginate_query(
        "bookings", q, order_field="booking_at", direction="asc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"bookings": items, "next_cursor": cursor})


@app.patch("/api/admin/bookings/<booking_id>")
@staff_required
@permission_required("manage_bookings")
def admin_booking_update(booking_id: str):
    p = current_principal()
    booking = get_document("bookings", validate_id(booking_id, "booking_id"), required=True, code="BOOKING_NOT_FOUND")
    data = payload_data()
    updates: dict[str, Any] = {}
    if "status" in data:
        status = str(data.get("status") or "").strip().lower()
        if status not in BOOKING_STATUSES:
            raise AppError("VALIDATION_ERROR", "Invalid booking status.", 422)
        updates["status"] = status
    if "internal_note" in data:
        updates["internal_note"] = validate_text(data.get("internal_note"), "internal_note", max_len=1000)
    if "date" in data or "time" in data:
        booking_date = parse_date_value(data.get("date", booking.get("date")), "date", True)
        booking_time = parse_time_value(data.get("time", booking.get("time")), "time", True)
        assert booking_date and booking_time
        updates["date"] = booking_date.isoformat()
        updates["time"] = booking_time.strftime("%H:%M")
        updates["booking_at"] = booking_datetime_local_to_utc(booking_date, booking_time)
    if "guests" in data:
        updates["guests"] = validate_int(data.get("guests"), "guests", 1, 100)
    if not updates:
        raise AppError("NO_CHANGES", "No booking changes were supplied.", 400)
    updates["updated_at"] = firestore.SERVER_TIMESTAMP
    updates["updated_by"] = p["uid"]
    require_db().collection("bookings").document(booking["id"]).set(updates, merge=True)
    audit_log("booking.updated", "booking", booking["id"], {"fields": sorted(updates.keys())})
    if "status" in updates:
        create_notification(
            recipient_uid=booking.get("customer_uid"),
            notification_type="transactional",
            title="Booking update",
            message=f"Booking {booking.get('booking_reference')} is now {updates['status']}.",
        )
    return api_success(get_document("bookings", booking["id"], required=True))


# =============================================================================
# Feedback / inquiry management
# =============================================================================

@app.get("/api/admin/feedback")
@staff_required
@permission_required("view_feedback")
def admin_feedback_list():
    filters: list[tuple[str, str, Any]] = []
    status = request.args.get("status", "").strip()
    if status:
        filters.append(("status", "==", validate_text(status, "status", required=True, max_len=40)))
    q = firestore_query("feedback", filters)
    items, cursor = paginate_query(
        "feedback", q, order_field="created_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"feedback": items, "next_cursor": cursor})


@app.patch("/api/admin/feedback/<feedback_id>")
@staff_required
@permission_required("manage_feedback")
def admin_feedback_update(feedback_id: str):
    feedback = get_document("feedback", validate_id(feedback_id, "feedback_id"), required=True, code="FEEDBACK_NOT_FOUND")
    data = payload_data()
    status = validate_text(data.get("status"), "status", required=True, max_len=40)
    note = validate_text(data.get("internal_note"), "internal_note", max_len=500)
    require_db().collection("feedback").document(feedback["id"]).set(
        {"status": status, "internal_note": note, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
    )
    audit_log("feedback.updated", "feedback", feedback["id"], {"status": status})
    return api_success(get_document("feedback", feedback["id"], required=True))


@app.get("/api/admin/inquiries")
@staff_required
@permission_required("view_feedback")
def admin_inquiries_list():
    filters: list[tuple[str, str, Any]] = []
    kind = request.args.get("kind", "").strip()
    if kind:
        filters.append(("kind", "==", validate_text(kind, "kind", required=True, max_len=40)))
    q = firestore_query("inquiries", filters)
    items, cursor = paginate_query(
        "inquiries", q, order_field="created_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"inquiries": items, "next_cursor": cursor})


# =============================================================================
# Reports and settings
# =============================================================================

def report_range() -> tuple[datetime, datetime]:
    today = business_now().date()
    start_day = parse_date_value(request.args.get("date_from") or (today - timedelta(days=30)).isoformat(), "date_from", True)
    end_day = parse_date_value(request.args.get("date_to") or today.isoformat(), "date_to", True)
    assert start_day and end_day
    if end_day < start_day:
        raise AppError("VALIDATION_ERROR", "date_to cannot be earlier than date_from.", 422)
    if (end_day - start_day).days > 366:
        raise AppError("VALIDATION_ERROR", "Report range cannot exceed 366 days.", 422)
    start = business_day_bounds(start_day)[0]
    end = business_day_bounds(end_day + timedelta(days=1))[0]
    return start, end


@app.get("/api/admin/reports")
@staff_required
@permission_required("view_reports")
def admin_reports_api():
    p = current_principal()
    start, end = report_range()
    base_filters = [("placed_at", ">=", start), ("placed_at", "<", end)]
    report: dict[str, Any] = {
        "orders": order_count(base_filters),
        "completed_orders": order_count(base_filters + [("order_status", "in", ["delivered", "picked_up", "completed"])]),
        "cancelled_orders": order_count(base_filters + [("order_status", "==", "cancelled")]),
        "delivery_orders": order_count(base_filters + [("service_mode", "==", "delivery")]),
        "pickup_orders": order_count(base_filters + [("service_mode", "==", "pickup")]),
        "dine_in_orders": order_count(base_filters + [("service_mode", "==", "dine_in")]),
        "range": {"start": start, "end_exclusive": end},
    }
    if staff_has_permission(p, "view_finance"):
        sales_filters = base_filters + [("payment_status", "==", "paid"), ("order_status", "!=", "cancelled")]
        sales_q = firestore_query("orders", sales_filters)
        revenue_minor = aggregate_sum(
            sales_q,
            "total_minor",
            fallback_collection="orders",
            fallback_filters=sales_filters,
        )
        paid_count = count_query(
            sales_q,
            fallback_collection="orders",
            fallback_filters=sales_filters,
        )
        report["revenue"] = money_fields(revenue_minor)
        report["average_order_value"] = money_fields(int(revenue_minor / paid_count) if paid_count else 0)
    return api_success(report)


def sanitize_settings_object(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        raise AppError("VALIDATION_ERROR", "Settings object is too deeply nested.", 422)
    forbidden_fragments = {"password", "secret", "private_key", "service_account", "id_token", "refresh_token", "merchant_key"}
    if isinstance(value, dict):
        if len(value) > 200:
            raise AppError("VALIDATION_ERROR", "Settings object has too many keys.", 422)
        out = {}
        for key, item in value.items():
            safe_key = validate_text(key, "settings_key", required=True, max_len=80)
            if any(fragment in safe_key.lower() for fragment in forbidden_fragments):
                raise AppError("VALIDATION_ERROR", f"Sensitive setting '{safe_key}' must remain in environment/server configuration.", 422)
            out[safe_key] = sanitize_settings_object(item, depth + 1)
        return out
    if isinstance(value, list):
        if len(value) > 200:
            raise AppError("VALIDATION_ERROR", "Settings list is too large.", 422)
        return [sanitize_settings_object(x, depth + 1) for x in value]
    if isinstance(value, str):
        return validate_text(value, "settings_value", max_len=2000)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    raise AppError("VALIDATION_ERROR", "Unsupported settings value type.", 422)


@app.get("/api/admin/settings/<settings_id>")
@staff_required
@permission_required("manage_settings")
def admin_settings_get(settings_id: str):
    settings_id = validate_id(settings_id, "settings_id")
    return api_success(get_document("settings", settings_id) or {"id": settings_id})


@app.patch("/api/admin/settings/<settings_id>")
@staff_required
@permission_required("manage_settings")
def admin_settings_update(settings_id: str):
    settings_id = validate_id(settings_id, "settings_id")
    raw = payload_data()
    if len(json.dumps(raw, default=str)) > 100_000:
        raise AppError("VALIDATION_ERROR", "Settings payload is too large.", 413)
    safe = sanitize_settings_object(raw)
    safe["updated_at"] = firestore.SERVER_TIMESTAMP
    safe["updated_by"] = current_principal()["uid"]
    require_db().collection("settings").document(settings_id).set(safe, merge=True)
    audit_log("settings.updated", "settings", settings_id, {"fields": sorted(k for k in safe if k not in {"updated_at", "updated_by"})})
    return api_success(get_document("settings", settings_id, required=True))


@app.get("/api/admin/audit-logs")
@staff_required
@permission_required("view_reports")
def admin_audit_logs():
    filters: list[tuple[str, str, Any]] = []
    action = request.args.get("action", "").strip()
    if action:
        filters.append(("action", "==", validate_text(action, "action", required=True, max_len=120)))
    q = firestore_query("audit_logs", filters)
    items, cursor = paginate_query(
        "audit_logs", q, order_field="created_at", direction="desc", limit=query_limit(), cursor=request.args.get("cursor")
    )
    return api_success({"audit_logs": items, "next_cursor": cursor})


# =============================================================================
# Firebase/Firestore bootstrap: roles, permissions, initial Owner
# =============================================================================

INITIAL_OWNER_EMAIL = os.getenv("INITIAL_OWNER_EMAIL", "ahmedkhanzaii00@gmail.com").strip().lower()


def bootstrap_reference_data() -> None:
    if DB is None:
        return
    db = require_db()
    try:
        batch = db.batch()
        for permission in sorted(PERMISSIONS):
            ref = db.collection("permissions").document(permission)
            batch.set(
                ref,
                {
                    "key": permission,
                    "active": True,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
            ref = db.collection("roles").document(role)
            batch.set(
                ref,
                {
                    "key": role,
                    "default_permissions": sorted(perms),
                    "rank": ROLE_RANK[role],
                    "active": True,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        batch.commit()
    except Exception:
        logger.exception("Reference role/permission bootstrap failed.")


def bootstrap_initial_owner() -> None:
    if DB is None or FIREBASE_APP is None:
        return
    if not INITIAL_OWNER_UID and not INITIAL_OWNER_EMAIL:
        logger.info("No INITIAL_OWNER_UID/INITIAL_OWNER_EMAIL configured; Owner bootstrap skipped.")
        return

    db = require_db()
    try:
        # Resolve the configured Firebase identity first. This allows the
        # bootstrap to repair a missing/incomplete staff document for an owner
        # that already exists, instead of returning too early.
        if INITIAL_OWNER_UID:
            user_record = auth.get_user(INITIAL_OWNER_UID, app=FIREBASE_APP)
        else:
            user_record = auth.get_user_by_email(INITIAL_OWNER_EMAIL, app=FIREBASE_APP)

        uid = user_record.uid
        email = (user_record.email or INITIAL_OWNER_EMAIL or "").lower()
        name = user_record.display_name or "Owner"

        active_owners = filtered_documents(
            "users",
            [("role", "==", ROLE_OWNER), ("status", "==", "active")],
            limit=2,
        )
        conflicting = [owner for owner in active_owners if owner.get("id") != uid]
        if conflicting:
            logger.warning(
                "An active Owner already exists (%s); refusing to bootstrap a different Owner (%s).",
                conflicting[0].get("id"),
                uid,
            )
            return

        batch = db.batch()
        batch.set(
            db.collection("users").document(uid),
            {
                "uid": uid,
                "full_name": name,
                "email": email,
                "role": ROLE_OWNER,
                "status": "active",
                "email_verified": bool(user_record.email_verified),
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        batch.set(
            db.collection("staff").document(uid),
            {
                "uid": uid,
                "full_name": name,
                "email": email,
                "role": ROLE_OWNER,
                "permissions": sorted(PERMISSIONS),
                "status": "active",
                "created_by": "server_bootstrap",
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        batch.commit()
        logger.warning("Initial Owner role securely bootstrapped for configured Firebase UID %s.", uid)
    except auth.UserNotFoundError:
        logger.error(
            "Owner bootstrap target does not exist in Firebase Authentication. Create the Firebase Auth account first, then restart."
        )
    except Exception:
        logger.exception("Initial Owner bootstrap failed.")


bootstrap_reference_data()
bootstrap_initial_owner()


# =============================================================================
# Error handlers
# =============================================================================

try:
    from jinja2 import TemplateNotFound
except Exception:  # Flask depends on Jinja2; defensive only
    TemplateNotFound = Exception  # type: ignore


def render_error_page(status: int, code: str, message: str):
    if wants_json_response():
        return api_error(code, message, status)
    try:
        return render_page(
            "error.html",
            status_code=status,
            error_code=code,
            error_message=message,
        ), status
    except TemplateNotFound:
        return f"{status} - {message}", status


@app.errorhandler(AppError)
def handle_app_error(exc: AppError):
    if exc.status >= 500:
        logger.error("Application error %s: %s", exc.code, exc.message)
    if wants_json_response():
        return api_error(exc.code, exc.message, exc.status, exc.details or None)
    try:
        return render_page(
            "error.html",
            status_code=exc.status,
            error_code=exc.code,
            error_message=exc.message,
            error_details=exc.details,
        ), exc.status
    except TemplateNotFound:
        return f"{exc.status} - {exc.message}", exc.status


@app.errorhandler(CSRFError)
def handle_csrf_error(exc: CSRFError):
    return render_error_page(400, "CSRF_FAILED", "The security token is missing, invalid, or expired. Refresh the page and try again.")


@app.errorhandler(400)
def handle_400(_exc):
    return render_error_page(400, "BAD_REQUEST", "The request could not be understood.")


@app.errorhandler(401)
def handle_401(_exc):
    return render_error_page(401, "AUTH_REQUIRED", "Authentication is required.")


@app.errorhandler(403)
def handle_403(_exc):
    return render_error_page(403, "ACCESS_DENIED", "You do not have permission to access this section. Please contact your Super Admin.")


@app.errorhandler(404)
def handle_404(_exc):
    return render_error_page(404, "NOT_FOUND", "The requested resource was not found.")


@app.errorhandler(405)
def handle_405(_exc):
    return render_error_page(405, "METHOD_NOT_ALLOWED", "This HTTP method is not allowed for this route.")


@app.errorhandler(413)
def handle_413(_exc):
    return render_error_page(413, "REQUEST_TOO_LARGE", "The request payload is too large.")


@app.errorhandler(429)
def handle_429(_exc):
    return render_error_page(429, "RATE_LIMITED", "Too many requests. Please try again later.")


@app.errorhandler(500)
def handle_500(exc):
    logger.exception("Unhandled application error: %s", exc)
    return render_error_page(500, "INTERNAL_SERVER_ERROR", "An unexpected server error occurred.")


# =============================================================================
# Startup
# =============================================================================

def _startup_route_sanity_check() -> None:
    required_routes = {
        "/admin/manage",
        "/admin/manage/<resource_key>",
        "/admin/manage/<resource_key>/new",
        "/admin/manage/<resource_key>/<record_id>/edit",
        "/admin/manage/products/<product_id>/options",
        "/admin/manage/products/<product_id>/gallery",
        "/product/<product_id>",
    }
    registered_routes = {rule.rule for rule in app.url_map.iter_rules()}
    missing_routes = sorted(required_routes - registered_routes)
    if missing_routes:
        raise RuntimeError(f"Required management routes are missing: {missing_routes}")

    required_route_methods = {
        ("/api/admin/products", "POST"),
        ("/api/admin/products/<product_id>", "PATCH"),
        ("/api/admin/products/<product_id>/options", "PUT"),
        ("/api/admin/products/<product_id>/gallery", "GET"),
        ("/api/admin/products/<product_id>/gallery", "PUT"),
        ("/api/admin/products/<product_id>/gallery/upload", "POST"),
    }
    registered_route_methods = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in rule.methods
    }
    missing_route_methods = sorted(required_route_methods - registered_route_methods)
    if missing_route_methods:
        raise RuntimeError(f"Required product API routes are missing: {missing_route_methods}")

    logger.info("Management routes registered successfully. build=%s", BUILD_TAG)


if __name__ == "__main__":
    _startup_route_sanity_check()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    debug_requested = os.getenv("FLASK_DEBUG", "false").lower() in {"1", "true", "yes", "on"}
    debug = bool(debug_requested and not IS_PRODUCTION)
    app.run(host=host, port=port, debug=debug)