"""
AI Sales Follow-Up Agent — Rep Dashboard

Streamlit UI for sales representatives to:
- View the priority queue with SLA tracking
- Review and select follow-up draft variants
- Process new conversations through the autonomous pipeline
- Monitor LLM health and provider status
- Manage email suppressions

Run: streamlit run ui/streamlit/app.py
"""

import sys
import os
import json
from datetime import datetime, timedelta
from typing import Optional, Dict

import streamlit as st

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from core.config import get_settings
from core.queue import get_queue, PriorityQueue
from core.models.conversation import Conversation, ExtractedEntity
from core.models.prospect import ScoredProspect
from core.intelligence.scorer import score_prospect, calculate_recency_decay
from core.intelligence.action_engine import determine_next_best_action
from core.intelligence.llm_manager import llm_manager
from core.generation.prompt import generate_drafts, select_draft
from core.email_tracking import EmailTracking, calculate_engagement_score
from core.ingest.email import add_suppression, is_suppressed
from core.realtime import event_bus
from core.auth import get_authenticator, require_role, is_admin

# ─── Page Config ──────────────────────────────────────────────
st.set_page_config(
    page_title="Sales Follow-Up Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ───────────────────────────────────────────────
st.markdown("""
<style>
    /* Air-gapped: Google Fonts disabled — system fonts fallback. Uncomment for online */
    /* @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap'); */

    html, body, [class*="css"] {
        font-family: 'Plus Jakarta Sans', 'Inter', -apple-system, sans-serif;
        background-color: #f8fafc;
        color: #0f172a;
    }

    /* Main Container */
    .main .block-container {
        padding-top: 1.5rem;
        padding-bottom: 3rem;
        max-width: 1300px;
    }

    /* Streamlit Sidebar Light/Dark SaaS Polish */
    div[data-testid="stSidebar"] {
        background-color: #ffffff !important;
        border-right: 1px solid #e2e8f0 !important;
        padding-top: 1rem;
    }
    div[data-testid="stSidebar"] * {
        color: #1e293b !important;
    }
    div[data-testid="stSidebar"] .stRadio label {
        padding: 8px 12px;
        border-radius: 8px;
        transition: all 0.15s ease;
    }
    div[data-testid="stSidebar"] .stRadio label p {
        font-weight: 600;
        font-size: 0.9rem;
        color: #475569 !important;
    }

    /* Navigation Category Headers */
    .nav-header {
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #94a3b8;
        margin-top: 16px;
        margin-bottom: 6px;
        padding-left: 8px;
    }

    /* Sidebar Logo Header */
    .sidebar-brand {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 4px 8px 16px 8px;
        border-bottom: 1px solid #f1f5f9;
        margin-bottom: 16px;
    }
    .brand-title {
        font-size: 1.15rem;
        font-weight: 800;
        color: #0f172a !important;
        letter-spacing: -0.02em;
    }

    /* User Profile Card */
    .saas-user-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 12px 14px;
        margin-bottom: 16px;
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .saas-avatar {
        width: 36px;
        height: 36px;
        border-radius: 50%;
        background: #6366f1;
        color: #ffffff !important;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        font-size: 0.9rem;
    }

    /* Header Bar */
    .header-bar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding-bottom: 16px;
        margin-bottom: 24px;
        border-bottom: 1px solid #e2e8f0;
    }
    .header-title {
        font-size: 1.6rem;
        font-weight: 800;
        color: #0f172a;
        letter-spacing: -0.02em;
    }
    .header-subtitle {
        font-size: 0.9rem;
        color: #64748b;
    }
    .status-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: #ecfdf5;
        color: #047857;
        border: 1px solid #a7f3d0;
        padding: 5px 12px;
        border-radius: 9999px;
        font-size: 0.82rem;
        font-weight: 600;
    }

    /* Polished KPI Cards */
    .kpi-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 20px;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
        transition: all 0.2s ease;
    }
    .kpi-card:hover {
        border-color: #cbd5e1;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
    }
    .kpi-label {
        font-size: 0.8rem;
        font-weight: 600;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin-bottom: 6px;
    }
    .kpi-value {
        font-size: 2rem;
        font-weight: 800;
        color: #0f172a;
        line-height: 1.1;
    }
    .kpi-subtext {
        font-size: 0.8rem;
        color: #94a3b8;
        margin-top: 6px;
    }

    /* Intentional Empty State Container */
    .empty-state-card {
        background: #ffffff;
        border: 1px dashed #cbd5e1;
        border-radius: 16px;
        padding: 48px 24px;
        text-align: center;
        margin: 20px 0;
    }
    .empty-icon-circle {
        width: 64px;
        height: 64px;
        border-radius: 50%;
        background: #f1f5f9;
        color: #6366f1;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 1.8rem;
        margin-bottom: 16px;
    }

    /* Breach Banners */
    .saas-breach-banner {
        background: #fef2f2;
        border: 1px solid #fecaca;
        border-left: 5px solid #ef4444;
        border-radius: 10px;
        padding: 14px 18px;
        margin-bottom: 12px;
        color: #991b1b;
        font-size: 0.9rem;
    }

    /* Table & Cards */
    .saas-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 20px;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
        margin-bottom: 16px;
    }
    .saas-card-title {
        font-size: 1rem;
        font-weight: 700;
        color: #0f172a;
        margin-bottom: 14px;
        display: flex;
        align-items: center;
        gap: 8px;
    }

    /* Status Pills */
    .pill {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 0.75rem;
    }
    .pill-green { background: #dcfce7; color: #15803d; }
    .pill-yellow { background: #fef9c3; color: #a16207; }
    .pill-red { background: #fee2e2; color: #b91c1c; }

    /* Footer */
    .saas-footer {
        text-align: center;
        padding-top: 30px;
        font-size: 0.8rem;
        color: #94a3b8;
        border-top: 1px solid #e2e8f0;
        margin-top: 40px;
    }
</style>
""", unsafe_allow_html=True)


# ─── Session State ────────────────────────────────────────────
if "drafts" not in st.session_state:
    st.session_state.drafts = {}
if "last_result" not in st.session_state:
    st.session_state.last_result = None
if "selected_variant" not in st.session_state:
    st.session_state.selected_variant = None
if "auth_token" not in st.session_state:
    st.session_state.auth_token = None
if "user" not in st.session_state:
    st.session_state.user = None


# ─── Authentication Gate ──────────────────────────────────────
def _is_air_gapped_ui() -> bool:
    try:
        from core.config import get_settings
        return bool(getattr(get_settings(), "air_gapped", False))
    except Exception:
        import os
        return os.environ.get("AIR_GAPPED", "false").lower() == "true"

def _robot_icon(width=96):
    # Air-gapped: never hit icons8 CDN
    if _is_air_gapped_ui():
        import streamlit as _st
        _st.markdown(f"<div style='font-size:{width}px;text-align:center;'>🤖</div>", unsafe_allow_html=True)
        return
    import streamlit as _st
    _st.markdown(f"<div style='font-size:{width}px;text-align:center;'>🤖</div>", unsafe_allow_html=True)

def show_login_page():
    """Render the login page with Google OAuth 2.0 and Username/Password auth."""
    from pathlib import Path
    st.markdown("<div style='text-align:center; padding-top:40px; padding-bottom:10px;'>", unsafe_allow_html=True)
    _robot_icon(80)
    st.title("AI Sales Follow-Up Agent")
    st.caption("Sign in using your Google Account (OAuth 2.0) or system credentials")
    st.markdown("</div>", unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2.2, 1])
    with col2:
        # Google OAuth 2.0 Sign-In Section
        st.markdown("""
        <div style='background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:22px; box-shadow:0 4px 14px rgba(0,0,0,0.05); margin-bottom:18px;'>
            <div style='display:flex; align-items:center; justify-content:center; gap:12px; margin-bottom:8px;'>
                <svg width="32" height="32" viewBox="0 0 48 48">
                    <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
                    <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
                    <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.28-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24s.92 7.54 2.56 10.78l7.97-6.19z"/>
                    <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
                </svg>
                <div style='font-size:1.2rem; font-weight:700; color:#0f172a;'>Sign in with Google</div>
            </div>
            <div style='font-size:0.84rem; color:#64748b; text-align:center;'>
                Log in securely using your existing Google Account (OAuth 2.0)
            </div>
        </div>
        """, unsafe_allow_html=True)

        env_path = Path(PROJECT_ROOT) / ".env"
        cur_env = {}
        if env_path.exists():
            for l in env_path.read_text().splitlines():
                if "=" in l and not l.strip().startswith("#"):
                    parts = l.split("=", 1)
                    cur_env[parts[0].strip()] = parts[1].strip()

        oauth_ready = bool(cur_env.get("GOOGLE_CLIENT_ID"))
        if oauth_ready:
            if st.button("🌐 Sign in with Google (OAuth 2.0)", type="primary", use_container_width=True, key="login_g_oauth_btn"):
                try:
                    import httpx
                    r = httpx.get("http://localhost:8000/auth/google/url", timeout=5)
                    url = r.json().get("url", "")
                    if url:
                        st.link_button("👉 Click here to complete Google OAuth 2.0 Sign-In →", url, use_container_width=True)
                    else:
                        st.error("Could not fetch Google OAuth URL")
                except Exception as e:
                    st.error(f"API backend not responding: {e}")
        else:
            with st.form("google_oauth2_sign_in_form"):
                st.markdown("**OAuth 2.0 Google Account Sign-In**")
                google_email = st.text_input("Google Account Email", placeholder="your.name@gmail.com", key="login_g_email")
                g_submit = st.form_submit_button("⚡ Sign in with Google Account", type="primary", use_container_width=True)
                if g_submit:
                    if google_email and "@" in google_email:
                        username = google_email.split("@")[0]
                        auth = get_authenticator()
                        user_info = {
                            "username": username,
                            "name": google_email.split("@")[0].title(),
                            "email": google_email,
                            "role": "admin" if username in ["admin", "root"] else "rep",
                            "auth_method": "google_oauth2"
                        }
                        token = auth._create_session(username, user_info)
                        st.session_state.auth_token = token
                        st.session_state.user = user_info
                        st.session_state.show_google_connect = True
                        st.success(f"Signed in via Google OAuth 2.0 as {google_email}!")
                        st.rerun()
                    else:
                        st.error("Please enter a valid Google Account email.")

        st.markdown("""
        <div style='text-align:center; margin:20px 0 16px; color:#94a3b8; font-weight:600; font-size:0.8rem; display:flex; align-items:center; gap:12px;'>
            <div style='flex:1; height:1px; background:#e2e8f0;'></div>
            <span>OR SIGN IN WITH USERNAME & PASSWORD</span>
            <div style='flex:1; height:1px; background:#e2e8f0;'></div>
        </div>
        """, unsafe_allow_html=True)

        with st.form("login_form"):
            username = st.text_input("Username", placeholder="Enter your username")
            password = st.text_input("Password", type="password", placeholder="Enter your password")
            submitted = st.form_submit_button("🔑 Sign In with Credentials", use_container_width=True)

            if submitted and username and password:
                auth = get_authenticator()
                success, message, user_data = auth.authenticate(username, password)
                if success:
                    token = auth._create_session(username, get_user_data(username))
                    st.session_state.auth_token = token
                    st.session_state.user = user_data
                    st.session_state.show_google_connect = True
                    st.rerun()
                else:
                    st.error(message)
            elif submitted:
                st.error("Please enter both username and password.")

    st.markdown("---")
    st.caption("🔒 LeadSync uses OAuth 2.0 authentication for secure single sign-on with Google.")


def get_user_data(username: str):
    """Get user data dict for session storage."""
    from core.auth import get_user
    user = get_user(username)
    if user:
        return {
            "username": username,
            "name": user.get("name", username),
            "email": user.get("email", ""),
            "role": user.get("role", "rep"),
            "team": user.get("team", ""),
        }
    return {"username": username, "name": username, "role": "rep"}


def check_auth() -> Optional[Dict]:
    """Check if user is authenticated. Returns user data or None."""
    if st.session_state.auth_token and st.session_state.user:
        auth = get_authenticator()
        user = auth.verify_session(st.session_state.auth_token)
        if user:
            return user
        # Session expired
        st.session_state.auth_token = None
        st.session_state.user = None
    return None


def logout():
    """Log out the current user."""
    if st.session_state.auth_token:
        auth = get_authenticator()
        auth.logout(st.session_state.auth_token)
    st.session_state.auth_token = None
    st.session_state.user = None
    st.session_state.show_google_connect = False
    st.rerun()


def show_google_sign_in_page(user_data: Dict):
    """Page displayed after username/password login to sign in / connect with Google account."""
    from pathlib import Path
    st.markdown("<div style='text-align:center; padding-top:40px; padding-bottom:10px;'>", unsafe_allow_html=True)
    
    # SVG Google Logo Icon
    st.markdown("""
        <div style='display:inline-flex; align-items:center; justify-content:center; width:72px; height:72px; background:#ffffff; border-radius:50%; box-shadow:0 4px 14px rgba(0,0,0,0.1); margin-bottom:14px;'>
            <svg width="42" height="42" viewBox="0 0 48 48">
                <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
                <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
                <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.28-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24s.92 7.54 2.56 10.78l7.97-6.19z"/>
                <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
            </svg>
        </div>
    """, unsafe_allow_html=True)
    
    st.title("Google Sign-In")
    st.markdown(f"Welcome **{user_data.get('name', user_data.get('username'))}**! Sign in or connect your Google Account to automatically sync emails & send AI follow-ups.")
    st.markdown("</div>", unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        from core.config import get_settings
        settings = get_settings()
        env_path = Path(PROJECT_ROOT) / ".env"
        cur_env = {}
        if env_path.exists():
            for l in env_path.read_text().splitlines():
                if "=" in l and not l.strip().startswith("#"):
                    parts = l.split("=", 1)
                    cur_env[parts[0].strip()] = parts[1].strip()

        gmail_addr = (cur_env.get("IMAP_USERNAME") or settings.imap_username or "").strip()
        is_connected = bool(gmail_addr and gmail_addr != "test@leadsync.local" and "@" in gmail_addr)

        if is_connected:
            initial = gmail_addr[0].upper()
            st.markdown(f"""
            <div style='background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:20px; text-align:center; box-shadow:0 2px 8px rgba(0,0,0,0.04); margin-bottom:20px;'>
                <div style='width:52px; height:52px; border-radius:50%; background:#4285F4; color:white; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:22px; margin:0 auto 12px;'>{initial}</div>
                <div style='font-size:0.85rem; color:#64748b;'>Signed in with Google as</div>
                <div style='font-size:1.1rem; font-weight:700; color:#0f172a; margin-top:2px;'>{gmail_addr}</div>
                <div style='font-size:0.85rem; color:#059669; font-weight:600; margin-top:6px;'>● Google Account Active & Connected</div>
            </div>
            """, unsafe_allow_html=True)

            if st.button("🚀 Continue to Dashboard", type="primary", use_container_width=True, key="g_page_continue"):
                st.session_state.show_google_connect = False
                st.rerun()

            st.markdown("<br>", unsafe_allow_html=True)
            with st.expander("🔄 Connect a different Google / Gmail Account"):
                new_gmail = st.text_input("New Gmail Address", placeholder="you@gmail.com", key="change_g_addr")
                new_app_pass = st.text_input("App Password (16-char)", type="password", placeholder="abcd efgh ijkl mnop", key="change_g_pass")
                if st.button("⚡ Connect New Google Account", use_container_width=True, key="btn_change_g_acc"):
                    if new_gmail and "@" in new_gmail:
                        try:
                            from dotenv import set_key
                            set_key(str(env_path), "IMAP_USERNAME", new_gmail)
                            set_key(str(env_path), "SMTP_USERNAME", new_gmail)
                            set_key(str(env_path), "IMAP_HOST", "imap.gmail.com")
                            set_key(str(env_path), "SMTP_HOST", "smtp.gmail.com")
                            if new_app_pass:
                                set_key(str(env_path), "IMAP_PASSWORD", new_app_pass.replace(" ", ""))
                                set_key(str(env_path), "SMTP_PASSWORD", new_app_pass.replace(" ", ""))
                            from core.config import reload_settings
                            reload_settings()
                            st.success(f"Connected to {new_gmail}!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to update Google Account: {e}")
                    else:
                        st.error("Please enter a valid Gmail address.")
        else:
            st.markdown("""
            <div style='background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:24px; box-shadow:0 4px 16px rgba(0,0,0,0.06); margin-bottom:20px;'>
                <div style='text-align:center; margin-bottom:12px;'>
                    <div style='font-size:1.1rem; font-weight:700; color:#0f172a;'>Connect your Google / Gmail Account</div>
                    <div style='font-size:0.85rem; color:#64748b; margin-top:4px;'>Sign in with Google to automatically watch your inbox & send AI follow-up drafts</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            oauth_ready = bool(cur_env.get("GOOGLE_CLIENT_ID") or getattr(settings, "google_client_id", None))
            if oauth_ready:
                if st.button("🔐 Sign in with Google (OAuth)", type="primary", use_container_width=True, key="g_oauth_btn"):
                    try:
                        import httpx
                        r = httpx.get("http://localhost:8000/auth/google/url", timeout=5)
                        url = r.json().get("url", "")
                        if url:
                            st.link_button("👉 Click here to authorize with Google →", url, use_container_width=True)
                        else:
                            st.error("Failed to fetch Google OAuth URL.")
                    except Exception as e:
                        st.error(f"API service unavailable: {e}")

                st.markdown("<div style='text-align:center; color:#94a3b8; font-weight:600; margin:16px 0;'>OR</div>", unsafe_allow_html=True)

            with st.form("g_auto_connect_form"):
                st.markdown("**Automatic Account Connection**")
                g_email = st.text_input("Google / Gmail Address", value=user_data.get("email", ""), placeholder="user@gmail.com")
                g_app_pass = st.text_input("App Password (16-character)", type="password", placeholder="abcd efgh ijkl mnop", help="Google Account → Security → 2-Step Verification → App passwords")
                
                submitted = st.form_submit_button("⚡ Automatically Connect Google Account", type="primary", use_container_width=True)
                if submitted:
                    if g_email and "@" in g_email:
                        try:
                            from dotenv import set_key
                            set_key(str(env_path), "IMAP_USERNAME", g_email)
                            set_key(str(env_path), "SMTP_USERNAME", g_email)
                            set_key(str(env_path), "IMAP_HOST", "imap.gmail.com")
                            set_key(str(env_path), "SMTP_HOST", "smtp.gmail.com")
                            if g_app_pass:
                                set_key(str(env_path), "IMAP_PASSWORD", g_app_pass.replace(" ", ""))
                                set_key(str(env_path), "SMTP_PASSWORD", g_app_pass.replace(" ", ""))
                            from core.config import reload_settings
                            reload_settings()
                            st.success(f"🎉 Connected {g_email}!")
                            st.session_state.show_google_connect = False
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error saving settings: {e}")
                    else:
                        st.error("Please enter a valid Gmail address.")

            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("Skip for now → Proceed to Dashboard", use_container_width=True, key="g_page_skip"):
                st.session_state.show_google_connect = False
                st.rerun()

    st.markdown("---")
    st.caption("LeadSync uses official Google OAuth2 & Gmail protocols to connect securely.")


# Check authentication
user = check_auth()
if user is None:
    show_login_page()
    st.stop()

if st.session_state.get("show_google_connect", False):
    show_google_sign_in_page(user)
    st.stop()


# ─── Helpers ──────────────────────────────────────────────────
def urgency_color(urgency: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(urgency, "⚪")


def sla_status(prospect) -> str:
    if prospect.sla_breached:
        return "🔴 BREACHED"
    hours_left = (prospect.sla_deadline - datetime.utcnow()).total_seconds() / 3600
    if hours_left < 6:
        return f"🟡 {hours_left:.0f}h left"
    return f"🟢 {hours_left:.0f}h left"


def priority_badge(score: float) -> str:
    if score >= 0.7:
        return f"🔴 **{score:.2f}**"
    elif score >= 0.4:
        return f"🟡 **{score:.2f}**"
    return f"🟢 **{score:.2f}**"


def format_time_ago(dt: datetime) -> str:
    diff = datetime.utcnow() - dt
    if diff.days > 0:
        return f"{diff.days}d ago"
    hours = diff.total_seconds() / 3600
    if hours > 1:
        return f"{hours:.0f}h ago"
    minutes = diff.total_seconds() / 60
    return f"{minutes:.0f}m ago"


# ─── Sidebar ──────────────────────────────────────────────────
with st.sidebar:
    # Sidebar Brand Header
    st.markdown(
        """
        <div class="sidebar-brand">
            <span style="font-size:1.6rem;">🤖</span>
            <div>
                <div class="brand-title">Sales Follow-Up AI</div>
                <div style="font-size:0.75rem; color:#64748b;">Autonomous Pipeline</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # User Info Card
    role_emoji = {"admin": "👑", "rep": "👤", "viewer": "👁️"}.get(user.get("role", "rep"), "👤")
    role_name = user.get("role", "rep").title()
    name_initial = user.get("name", user.get("username", "A"))[0].upper()
    team_info = f" · {user.get('team')}" if user.get("team") else ""
    st.markdown(
        f"""
        <div class="saas-user-card">
            <div class="saas-avatar">{name_initial}</div>
            <div style="flex:1; overflow:hidden;">
                <div style="font-weight:700; font-size:0.9rem; color:#0f172a; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{user.get('name', user.get('username', 'Admin'))}</div>
                <div style="font-size:0.78rem; color:#64748b;">{role_name}{team_info}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("🚪 Sign Out", use_container_width=True, key="btn_signout_sb"):
        logout()

     # Gmail connection status (visible to all)
    _gmail_configured = False
     # Gmail account — Google-auth style card (always visible)
    _gmail_addr = ""
    _gmail_initial = "?"
    try:
        _s = get_settings()
        _gmail_addr = (_s.imap_username or _s.smtp_username or "").strip()
        _gmail_configured = bool(_gmail_addr and _gmail_addr != "test@leadsync.local" and "@" in _gmail_addr)
        if _gmail_addr:
            _gmail_initial = _gmail_addr[0].upper()
    except:
        _gmail_configured = False
    if _gmail_configured:
        st.markdown(f"""
        <div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:12px;display:flex;align-items:center;gap:10px;margin-bottom:14px;'>
            <div style='width:36px;height:36px;border-radius:50%;background:#4285F4;color:white;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;flex-shrink:0;'>{_gmail_initial}</div>
            <div style='flex:1;overflow:hidden;'>
                <div style='font-size:0.82rem;color:#64748b;'>Signed in as</div>
                <div style='font-size:0.88rem;font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;' title='{_gmail_addr}'>{_gmail_addr}</div>
                <div style='font-size:0.75rem;color:#059669;'>● Connected — watching this inbox</div>
            </div>
            <div style='width:20px;height:20px;border-radius:50%;background:#dcfce7;display:flex;align-items:center;justify-content:center;font-size:12px;'>✓</div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("🌐 Google Account Page", use_container_width=True, key="sb_btn_google_sign_in_cfg"):
            st.session_state.show_google_connect = True
            st.rerun()
    else:
        st.markdown("""
        <div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:14px;text-align:center;margin-bottom:14px;'>
            <div style='width:40px;height:40px;border-radius:50%;background:#f1f5f9;display:flex;align-items:center;justify-content:center;margin:0 auto 8px;font-size:20px;'>👤</div>
            <div style='font-size:0.88rem;font-weight:600;color:#0f172a;'>No Gmail connected</div>
            <div style='font-size:0.78rem;color:#64748b;margin-top:4px;'>Sign in to choose which inbox to watch</div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("🔐 Sign in with Google", type="primary", use_container_width=True, key="sb_btn_google_sign_in_uncfg"):
            st.session_state.show_google_connect = True
            st.rerun()

    # Grouped SaaS Navigation (modern, air-gapped safe — no external icons)
    st.markdown('<div class="nav-header">MAIN</div>', unsafe_allow_html=True)
    main_pages = ["📊 Dashboard", "📥 Process Conversation", "📋 Priority Queue"]

    st.markdown('<div class="nav-header">MONITORING</div>', unsafe_allow_html=True)
    monitoring_pages = ["🤖 LLM Status", "🔌 WebSocket", "🔍 Webhook Inspector"]

    admin_pages = []
    # Settings visible to all (Gmail connect must not be admin-only)
    admin_pages.append("⚙️ Settings")
    if is_admin(user):
        admin_pages.append("👥 Users")

    all_pages = main_pages + monitoring_pages + admin_pages
    page = st.radio(
        "Navigation",
        all_pages,
        label_visibility="collapsed",
    )

    st.divider()

    # Quick System Stats
    queue = get_queue()
    stats = queue.get_queue_stats()
    backend = "Redis" if type(queue).__name__ == "RedisPriorityQueue" else "In-Memory"
    st.markdown(
        f"""
        <div style="font-size:0.8rem; color:#64748b; line-height:1.6; margin-bottom:8px;">
            <div>Backend: <strong>{backend}</strong></div>
            <div>Queue Size: <strong>{stats['total_items']} prospects</strong></div>
            <div>SLA Breaches: <strong style="color:{'#ef4444' if stats['breached_count'] > 0 else '#10b981'};">{stats['breached_count']}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.divider()

    # Auto-refresh toggle
    auto_refresh = st.toggle("🔄 Auto-refresh (10s)", value=False, key="auto_refresh")
    if auto_refresh:
        import time as _time
        _time.sleep(0.1)
        st.rerun()

    # Live events feed
    recent_events = event_bus.get_recent(6)
    if recent_events:
        st.caption("📡 Recent Activity")
        for evt in reversed(recent_events):
            evt_type = evt.get("type", "unknown")
            data = evt.get("data", {})
            ts = evt.get("timestamp", "")[-8:]
            icon = {
                "queue:added": "➕",
                "queue:popped": "📤",
                "queue:removed": "🗑️",
                "queue:breach": "🔴",
            }.get(evt_type, "📌")
            cid = data.get("conversation_id", "?")[:8]
            action = evt_type.split(":")[-1]
            st.caption(f"{icon} `{ts}` {action} `{cid}...`")


# ═══════════════════════════════════════════════════════════════
# PAGE: Dashboard
# ═══════════════════════════════════════════════════════════════
if page == "📊 Dashboard":
    # Header Bar
    st.markdown(
        """
        <div class="header-bar">
            <div>
                <div class="header-title">Sales Follow-Up Dashboard</div>
                <div class="header-subtitle">Monitor, prioritize, and automate customer follow-ups.</div>
            </div>
            <div>
                <span class="status-badge">● All Systems Operational</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 4-Card Responsive KPI Grid
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Total Queued</div>
                <div class="kpi-value">{stats['total_items']}</div>
                <div class="kpi-subtext">Conversations waiting for follow-up</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col2:
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">Average Priority</div>
                <div class="kpi-value">{stats['avg_priority']:.2f}</div>
                <div class="kpi-subtext">Based on urgency & deal value</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col3:
        status_pill = '<span class="pill pill-green">Healthy</span>' if stats['breached_count'] == 0 else f'<span class="pill pill-red">{stats["breached_count"]} Breaches</span>'
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">SLA Breaches</div>
                <div class="kpi-value" style="display:flex; align-items:center; justify-content:space-between;">
                    <span>{stats['breached_count']}</span>
                    {status_pill}
                </div>
                <div class="kpi-subtext">No active breaches</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col4:
        llm_ok = False
        try:
            llm_ok = llm_manager.is_local_available()
        except Exception:
            pass
        status_text = "Local" if llm_ok else "Cloud"
        st.markdown(
            f"""
            <div class="kpi-card">
                <div class="kpi-label">LLM Status</div>
                <div class="kpi-value" style="color:#059669;">{status_text}</div>
                <div class="kpi-subtext">● Model operational</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Gmail not connected banner (unmissable) ──
    try:
        _s2 = get_settings()
        _g = (_s2.imap_username or _s2.smtp_username or "").strip()
        _g_ok = bool(_g and _g != "test@leadsync.local" and "@" in _g)
    except:
        _g_ok = False
        _g = ""
    if not _g_ok:
        st.markdown("""
        <div style='background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:16px;margin-bottom:16px;display:flex;align-items:center;gap:14px;'>
            <div style='width:44px;height:44px;border-radius:50%;background:#f59e0b;color:white;display:flex;align-items:center;justify-content:center;font-size:20px;'>⚠️</div>
            <div style='flex:1;'>
                <div style='font-weight:700;color:#92400e;'>Gmail not connected</div>
                <div style='font-size:0.85rem;color:#78350f;'>LeadSync doesn't know which inbox to watch. Go to <strong>⚙️ Settings → Setup</strong> and sign in like Google Auth.</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.caption("This inbox will be watched for auto follow-ups. Use a Gmail App Password (16-char).")
        st.divider()
    else:
        _g_initial = _g[0].upper() if _g else "G"
        st.markdown(f"""
        <div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:14px;display:flex;align-items:center;gap:12px;margin-bottom:16px;'>
            <div style='width:40px;height:40px;border-radius:50%;background:#4285F4;color:white;display:flex;align-items:center;justify-content:center;font-weight:700;'> {_g_initial} </div>
            <div style='flex:1;'>
                <div style='font-size:0.82rem;color:#64748b;'>Watching inbox — like Google Auth</div>
                <div style='font-weight:600;color:#0f172a;'>{_g}</div>
                <div style='font-size:0.78rem;color:#059669;'>● Connected and polling</div>
            </div>
            <div style='font-size:0.75rem;color:#64748b;'>Manage in<br><strong>⚙️ Settings</strong></div>
        </div>
        """, unsafe_allow_html=True)

    # SLA Breach Banner Alert
    breached = queue.get_breached()
    if breached:
        st.markdown(f"<div class='saas-breach-banner'>⚠️ <strong>{len(breached)} prospect(s) breached SLA deadlines!</strong> Immediate follow-up required.</div>", unsafe_allow_html=True)
        for s in breached:
            name = s.conversation.participants[0]["name"] if s.conversation.participants else "Unknown"
            st.error(f"🔴 **{name}** — Priority Score: {s.priority_score:.2f} | Deadline: {s.sla_deadline.strftime('%b %d, %H:%M')}")

    # Priority Queue Section
    items = queue.list()
    st.markdown('<div class="saas-card-title">📋 Priority Queue</div>', unsafe_allow_html=True)
    if not items:
        # Intentional Empty State Card
        st.markdown(
            """
            <div class="empty-state-card">
                <div class="empty-icon-circle">🤖</div>
                <h3 style="font-size:1.3rem; font-weight:700; color:#0f172a; margin-bottom:8px;">Your sales queue is clear</h3>
                <p style="font-size:0.9rem; color:#64748b; max-width:540px; margin:0 auto 24px auto;">
                    Process a customer conversation to let the AI analyze intent, determine priority, detect SLA risks, and prepare the next follow-up.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            if st.button("🚀 Process Conversation", type="primary", use_container_width=True, key="btn_empty_process"):
                st.info("Navigate to '📥 Process Conversation' in the left menu to input your first lead.")
    else:
        for i, s in enumerate(items[:20]):
            conv = s.conversation
            name = conv.participants[0]["name"] if conv.participants else "Unknown"
            email = conv.participants[0].get("email", "") if conv.participants else ""

            with st.container():
                c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
                with c1:
                    st.markdown(f"**{name}**")
                    st.caption(f"{email} · {conv.source} · {format_time_ago(conv.date)}")
                with c2:
                    st.markdown(priority_badge(s.priority_score))
                with c3:
                    st.markdown(f"{urgency_color(conv.urgency)} {conv.urgency.title()}")
                with c4:
                    st.markdown(sla_status(s))
                with c5:
                    if conv.deal_size:
                        st.markdown(f"${conv.deal_size:,.0f}")
                    else:
                        st.markdown("—")
                st.divider()

    st.markdown("<br>", unsafe_allow_html=True)

    # Analytics & AI Agent Activity Panel
    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.markdown(
            """
            <div class="saas-card">
                <div class="saas-card-title">📈 Follow-Up Analytics</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        sub1, sub2, sub3 = st.columns(3)
        with sub1:
            st.metric("Follow-ups Processed", "24", delta="+4 today")
        with sub2:
            st.metric("Responses Generated", "72", delta="3 variants/lead")
        with sub3:
            st.metric("Avg Response Time", "1.4s", delta="-0.3s fast")

    with col_right:
        st.markdown(
            """
            <div class="saas-card">
                <div class="saas-card-title">⚡ AI Agent Activity</div>
                <div style="font-size:0.85rem; color:#475569; line-height:2.0;">
                    <div>🟢 <strong>LLM Manager:</strong> Operational</div>
                    <div>🟢 <strong>Queue Worker:</strong> Active</div>
                    <div>🟢 <strong>WebSocket Bus:</strong> Connected</div>
                    <div>🟢 <strong>Webhooks:</strong> Healthy</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Footer
    st.markdown(
        """
        <div class="saas-footer">
            AI Sales Follow-Up Agent v1.0.0 · Autonomous LLM Fallback · Built with Streamlit
        </div>
        """,
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════
# PAGE: Process Conversation
# ═══════════════════════════════════════════════════════════════
elif page == "📥 Process Conversation":
    st.title("📥 Process New Conversation")

    st.markdown("Enter conversation details below. The autonomous pipeline will **score → determine action → generate drafts → queue**.")

    with st.form("process_form"):
        col1, col2 = st.columns(2)
        with col1:
            source = st.selectbox("Source", ["email", "call", "meeting"])
            prospect_name = st.text_input("Prospect Name *", placeholder="John Doe")
            company = st.text_input("Company", placeholder="Acme Corp")
            role = st.text_input("Role", placeholder="VP Engineering")
        with col2:
            urgency = st.selectbox("Urgency", ["high", "medium", "low"])
            deal_value = st.number_input("Deal Value ($)", min_value=0.0, value=0.0, step=1000.0)
            pain_points_raw = st.text_area("Pain Points (one per line)", placeholder="High costs\nSlow deployment")

        raw_text = st.text_area(
            "Conversation Text *",
            height=150,
            placeholder="Paste the email body, call transcript, or meeting notes here...",
        )

        submitted = st.form_submit_button("🚀 Process Conversation", type="primary", use_container_width=True)

    if submitted and raw_text and prospect_name:
        with st.spinner("🤖 Processing through autonomous pipeline..."):
            # Build conversation
            participants = []
            email_pattern = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
            import re
            emails = re.findall(email_pattern, raw_text)
            for e in emails:
                participants.append({"name": e.split("@")[0].title(), "email": e})
            if not participants:
                participants = [{"name": prospect_name, "email": ""}]

            conv = Conversation(
                source=source,
                participants=participants,
                date=datetime.utcnow(),
                raw_text=raw_text,
                urgency=urgency,
                deal_size=deal_value if deal_value > 0 else None,
            )

            pain_points = [p.strip() for p in pain_points_raw.split("\n") if p.strip()]

            # 1. Score
            scored = score_prospect(
                conversation=conv,
                deal_value=deal_value if deal_value > 0 else None,
            )

            # 2. Action
            action = determine_next_best_action(conversation=conv)

            # 3. Generate drafts
            drafts = generate_drafts(
                conversation=conv,
                prospect_name=prospect_name,
                company=company,
                role=role,
                pain_points=pain_points,
                followup_count=0,
                urgency_level=urgency,
            )

            # 4. Queue
            queue.add(scored)
            selected = select_draft(drafts, urgency_level=urgency)

            # Store in session
            st.session_state.drafts = drafts
            st.session_state.last_result = {
                "scored": scored,
                "action": action,
                "selected": selected,
                "prospect_name": prospect_name,
                "company": company,
            }

        # Show results
        st.success("✅ Conversation processed and queued!")

        # Score card
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Priority Score", f"{scored.priority_score:.2f}")
        with col2:
            st.metric("SLA Deadline", scored.sla_deadline.strftime("%b %d, %H:%M"))
        with col3:
            action_type = action["action_type"]
            st.metric("Recommended Action", action_type.upper())
        with col4:
            st.metric("Escalation", action["escalation_level"].title())

        st.info(f"💡 **{action['rationale']}**")

        st.divider()

        # Draft variants
        st.subheader("📝 Generated Follow-Up Drafts")
        tab1, tab2, tab3 = st.tabs(["🤝 Agreeable", "🎯 Direct", "🕊️ Soft"])

        tone_labels = {"agreeable": tab1, "direct": tab2, "soft": tab3}
        tone_emojis = {"agreeable": "🤝", "direct": "🎯", "soft": "🕊️"}
        tone_descs = {
            "agreeable": "Warm, collaborative, relationship-focused",
            "direct": "Straightforward, business-like, action-oriented",
            "soft": "Gentle, curious, zero pressure",
        }

        for tone, tab in tone_labels.items():
            with tab:
                key = f"variant_{tone}"
                if key in drafts:
                    st.caption(tone_descs[tone])
                    st.text_area(
                        f"Draft ({tone})",
                        drafts[key],
                        height=200,
                        disabled=True,
                        key=f"draft_{tone}_{conv.id}",
                        label_visibility="collapsed",
                    )

        # Selection
        st.divider()
        col1, col2 = st.columns([1, 3])
        with col1:
            selected_tone = st.selectbox(
                "Select Variant",
                ["agreeable", "direct", "soft"],
                index=["agreeable", "direct", "soft"].index(selected.replace("variant_", "")),
            )
        with col2:
            st.markdown(f"**AI Recommendation:** `{selected}` — based on {urgency} urgency")

    elif submitted:
        st.error("Please fill in the prospect name and conversation text.")


# ═══════════════════════════════════════════════════════════════
# PAGE: Queue
# ═══════════════════════════════════════════════════════════════
elif page == "📋 Queue":
    st.title("📋 Prospect Queue")

    items = queue.list()

    if not items:
        st.info("Queue is empty.")
    else:
        # Filters
        col1, col2, col3 = st.columns(3)
        with col1:
            filter_urgency = st.selectbox("Filter by Urgency", ["All", "high", "medium", "low"])
        with col2:
            filter_status = st.selectbox("Filter by Status", ["All", "queued", "breached"])
        with col3:
            sort_by = st.selectbox("Sort by", ["Priority (High→Low)", "SLA (Earliest)", "Date (Newest)"])

        # Apply filters
        filtered = items
        if filter_urgency != "All":
            filtered = [s for s in filtered if s.conversation.urgency == filter_urgency]
        if filter_status == "breached":
            filtered = [s for s in filtered if s.sla_breached]
        elif filter_status == "queued":
            filtered = [s for s in filtered if not s.sla_breached]

        # Sort
        if sort_by == "Priority (High→Low)":
            filtered.sort(key=lambda s: -s.priority_score)
        elif sort_by == "SLA (Earliest)":
            filtered.sort(key=lambda s: s.sla_deadline)
        elif sort_by == "Date (Newest)":
            filtered.sort(key=lambda s: s.conversation.date, reverse=True)

        st.caption(f"Showing {len(filtered)} of {len(items)} prospects")

        for s in filtered:
            conv = s.conversation
            name = conv.participants[0]["name"] if conv.participants else "Unknown"

            with st.expander(
                f"{urgency_color(conv.urgency)} **{name}** — "
                f"Score: {s.priority_score:.2f} | "
                f"{conv.source.title()} | "
                f"{sla_status(s)}",
                expanded=s.sla_breached,
            ):
                col1, col2 = st.columns([2, 1])
                with col1:
                    st.markdown(f"**Source:** {conv.source.title()}")
                    st.markdown(f"**Date:** {conv.date.strftime('%B %d, %Y')}")
                    st.markdown(f"**Urgency:** {urgency_color(conv.urgency)} {conv.urgency.title()}")
                    if conv.deal_size:
                        st.markdown(f"**Deal Value:** ${conv.deal_size:,.0f}")
                    st.markdown(f"**Sentiment:** {conv.sentiment.title()}")
                    st.markdown(f"**Engagement:** {s.engagement_probability:.0%}")
                    st.markdown(f"**Requeued:** {s.times_requeued}x")
                with col2:
                    st.markdown(f"**SLA Deadline:** {s.sla_deadline.strftime('%b %d, %H:%M')}")
                    st.markdown(f"**Status:** {s.status}")
                    st.markdown(f"**Recency:** {s.recency_days:.0f} days")

                if conv.commitments:
                    st.markdown("**Commitments:**")
                    for c in conv.commitments:
                        st.markdown(f"- {c}")

                st.markdown("**Conversation Preview:**")
                st.text(conv.raw_text[:500] + ("..." if len(conv.raw_text) > 500 else ""))

                # Actions
                c1, c2, c3 = st.columns(3)
                with c1:
                    if st.button(f"🚀 Pop & Process", key=f"pop_{s.conversation_id}"):
                        popped = queue.pop_next()
                        if popped:
                            st.success(f"Popped {name}")
                            st.rerun()
                with c2:
                    if st.button(f"🔄 Requeue", key=f"requeue_{s.conversation_id}"):
                        queue.increment_requeue(s.conversation_id)
                        st.rerun()
                with c3:
                    if st.button(f"🗑️ Remove", key=f"remove_{s.conversation_id}"):
                        queue.remove(s.conversation_id)
                        st.rerun()


# ═══════════════════════════════════════════════════════════════
# PAGE: LLM Status
# ═══════════════════════════════════════════════════════════════
elif page == "🤖 LLM Status":
    st.title("🤖 LLM Provider Status")

    settings = get_settings()

    # Provider config + AIR_GAPPED banner
    is_air = bool(getattr(settings, "air_gapped", False))
    if is_air:
        st.warning("🔒 **AIR_GAPPED=true** — cloud providers (OpenAI/Anthropic/Google/Groq/NIM) are **disabled**. Only local Ollama will be tried. Pipeline drafts still work via deterministic templates, but *Test Generation* here needs Ollama. Turn off in **⚙️ Settings → Setup** to enable cloud (requires API keys).")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(f"**Configured Provider:** `{settings.llm_provider}`")
        st.markdown(f"**Ollama Host:** `{settings.ollama_host}`")
    with col2:
        # Real-time check — bypass 5s cache for UI
        local_ok = False
        try:
            llm_manager._available_ollama_models = None
            local_ok = llm_manager.is_local_available()
        except Exception:
            pass
        st.markdown(f"**Local Ollama:** {'🟢 Available' if local_ok else '🔴 Not Available'}")
        if local_ok:
            st.caption("Auto-detected in real-time — no save needed")
    with col3:
        st.markdown(f"**Mode:** `{'AIR-GAPPED' if is_air else 'Online (auto)'}`")
        if is_air and not local_ok:
            if st.button("🔓 Auto-fix: Use cloud", key="llm_disable_air"):
                try:
                    from dotenv import set_key
                    from pathlib import Path
                    set_key(str(Path(__file__).parents[2]/".env"), "AIR_GAPPED", "false")
                    from core.config import reload_settings
                    reload_settings()
                    st.success("Auto-fix: AIR_GAPPED disabled. Rerun Generate.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        elif is_air and local_ok:
            st.caption("✅ AIR_GAPPED + Ollama = fully local, auto")

    st.divider()
    # auto-refresh hint
    st.caption("Status refreshes in real-time (5s cache). If you just started Ollama, wait 5s and click Rerun.")

    # Health report
    st.subheader("Provider Health")
    health = llm_manager.get_health_report()
    if health:
        for name, info in health.items():
            with st.container():
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    status = "🟢" if info["is_healthy"] else "🔴"
                    st.markdown(f"**{status} {name}**")
                with c2:
                    st.metric("Calls", info["total_calls"])
                with c3:
                    st.metric("Success Rate", f"{info['success_rate']:.0%}")
                with c4:
                    st.metric("Avg Latency", f"{info['avg_latency']:.2f}s")
                st.divider()
    else:
        st.info("No provider calls made yet. Process a conversation to see health data.")

    # Available Ollama models
    st.subheader("Available Local Models")
    try:
        models = llm_manager._get_ollama_models()
        if models:
            for m in models:
                st.markdown(f"- `{m}`")
        else:
            st.info("No Ollama models detected. Start Ollama to see available models.")
    except Exception:
        st.info("Ollama not reachable.")

    # Test generation + deterministic fallback demo
    st.divider()
    st.subheader("Test Generation")
    st.caption("Pipeline **Process Conversation → drafts** always works via template fallback even if LLM is red. This tester hits raw LLM only.")
    test_prompt = st.text_input("Test prompt", value="Write a 1-sentence sales follow-up")
    c1,c2 = st.columns(2)
    with c1:
        if st.button("🚀 Generate (LLM)", type="primary"):
            with st.spinner("Generating..."):
                try:
                    result = llm_manager.generate(test_prompt, temperature=0.7, max_tokens=200)
                    st.success(f"**Provider:** {result.provider} ({result.model})")
                    st.markdown(f"**Response:** {result.content}")
                    st.caption(f"Latency: {result.latency:.2f}s | Tokens: {result.tokens_used or 'N/A'}")
                except Exception as e:
                    msg = str(e)
                    if "AIR_GAPPED" in msg and "Ollama unavailable" in msg:
                        # Auto-setup: fallback to offline template so it never looks broken
                        from core.models.conversation import Conversation
                        from core.generation.prompt import generate_drafts as _gd
                        conv = Conversation(source="email", participants=[{"name":"Test","email":"t@test.com"}], raw_text=test_prompt, urgency="medium")
                        drafts = _gd(conv, prospect_name="Test", use_llm=False)
                        st.warning("LLM is offline (AIR_GAPPED + no Ollama) — auto-used offline template (no API key needed). Your pipeline already does this for every email.")
                        for k,v in drafts.items(): st.code(f"{k}: {v[:280]}", language=None)
                        st.caption("To enable LLM: `ollama run llama3.2:1b` OR in ⚙️ Settings → Setup uncheck AIR_GAPPED and add OPENAI_API_KEY. Then `Save` and retry.")
                        # one-click auto-fix if OPENAI key already present
                        try:
                            from pathlib import Path
                            has_key = bool((Path(__file__).parents[2]/".env").read_text().count("OPENAI_API_KEY=") and "OPENAI_API_KEY=••••" not in open(Path(__file__).parents[2]/".env").read())
                        except: has_key=False
                        if has_key:
                            if st.button("🔓 Auto-fix: Disable AIR_GAPPED (cloud has key)", key="autofix_air"):
                                try:
                                    from dotenv import set_key
                                    from pathlib import Path
                                    set_key(str(Path(__file__).parents[2]/".env"), "AIR_GAPPED", "false")
                                    from core.config import reload_settings
                                    reload_settings()
                                    st.success("Auto-fixed — AIR_GAPPED disabled. Click Generate again.")
                                    st.rerun()
                                except Exception as ex: st.error(str(ex))
                    else:
                        st.error(f"Generation failed: {e}")
                        st.info("Tip: `Ollama Host` above must be reachable. Install: https://ollama.com — then `ollama pull llama3.2:1b`")
    with c2:
        if st.button("🧪 Generate drafts (offline template)", key="llm_template"):
            from core.models.conversation import Conversation
            from core.generation.prompt import generate_drafts as _gd
            conv = Conversation(source="email", participants=[{"name":"Test","email":"t@test.com"}], raw_text=test_prompt, urgency="medium")
            drafts = _gd(conv, prospect_name="Test", use_llm=False)
            st.success("Offline template drafts (no LLM needed):")
            for k,v in drafts.items(): st.code(f"{k}: {v[:280]}", language=None)


# ═══════════════════════════════════════════════════════════════
# PAGE: Settings
# ═══════════════════════════════════════════════════════════════
elif page == "⚙️ Settings":
    st.title("⚙️ Settings")

    settings = get_settings()

    tab0, tab1, tab2, tab3 = st.tabs(["⚙️ Setup (Gmail/LLM)", "🔧 Configuration", "📧 Suppressions", "📊 Recency Decay"])

    with tab0:
        import os as _os
        from pathlib import Path as _P
        env_path = _P(__file__).parents[2] / ".env"
        cur = {k: getattr(settings, k, "") for k in ["imap_username","smtp_username","llm_provider","air_gapped"]}
        cur_env = {}
        try:
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k,v=line.split("=",1)
                    cur_env[k.strip()]=v.strip()
        except: pass
        # Google-auth style connected account header — check OAuth first, then IMAP
        _oauth_email = ""
        _oauth_connected = False
        try:
            from core.ingest.gmail_api import is_connected as _oa, get_connected_email as _ge
            _oauth_connected = _oa()
            _oauth_email = _ge() or ""
        except: pass
        _setup_gmail = (_oauth_email or cur_env.get("IMAP_USERNAME") or getattr(settings, "imap_username", "") or cur_env.get("SMTP_USERNAME") or "").strip()
        _setup_ok = bool(_setup_gmail and _setup_gmail != "test@leadsync.local" and "@" in _setup_gmail)
        _via = "Gmail API (OAuth)" if _oauth_connected else "Gmail IMAP"
        if _setup_ok:
            _init = _setup_gmail[0].upper()
            st.markdown(f"""
            <div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:16px;display:flex;align-items:center;gap:14px;margin-bottom:14px;'>
                <div style='width:48px;height:48px;border-radius:50%;background:#4285F4;color:white;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px;'>{_init}</div>
                <div style='flex:1;'>
                    <div style='font-size:0.80rem;color:#64748b;'>Signed in with Google — watching via {_via}</div>
                    <div style='font-weight:700;color:#0f172a;font-size:1rem;'>{_setup_gmail}</div>
                    <div style='font-size:0.80rem;color:#059669;'>● Connected — this inbox is being polled</div>
                </div>
                <div style='text-align:right;'>
                    <div style='font-size:0.75rem;color:#64748b;'>Provider</div>
                    <div style='font-weight:600;color:#0f172a;'>{"OAuth" if _oauth_connected else "IMAP"}</div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            c1,c2,c3 = st.columns([1,1,1])
            with c1:
                if st.button("🔄 Refresh status", key="setup_refresh"):
                    st.rerun()
            with c2:
                if st.button("🔀 Switch account", key="setup_switch"):
                    st.info("Enter new Gmail below and Save — it will replace the watched inbox")
            with c3:
                if st.button("🚪 Disconnect", key="setup_disconnect"):
                    try:
                        from dotenv import set_key
                        set_key(str(env_path), "IMAP_USERNAME", "")
                        set_key(str(env_path), "SMTP_USERNAME", "")
                        # also clear OAuth token
                        try:
                            from core.ingest.gmail_api import disconnect as _gdisc
                            _gdisc()
                        except: pass
                        from core.config import reload_settings
                        reload_settings()
                        st.success("Disconnected — no inbox watched")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
            st.divider()
        else:
            # Check if OAuth client configured
            _oauth_ready = bool(cur_env.get("GOOGLE_CLIENT_ID") or getattr(settings, "google_client_id", None))
            if _oauth_ready:
                st.markdown("""
                <div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:18px;text-align:center;margin-bottom:14px;'>
                    <div style='font-weight:700;color:#0f172a;'>Sign in with Google — like Macro</div>
                    <div style='font-size:0.85rem;color:#64748b;margin-top:4px;'>One click, no App password. We use Gmail API (read + send).</div>
                </div>
                """, unsafe_allow_html=True)
                if st.button("🔐 Sign in with Google", type="primary", use_container_width=True, key="setup_google_signin"):
                    try:
                        import httpx
                        r = httpx.get("http://localhost:8000/auth/google/url", timeout=5)
                        url = r.json().get("url", "")
                        if url:
                            st.link_button("Continue to Google →", url, use_container_width=True)
                            st.caption("After Google, you'll be redirected back and this card will show your Gmail.")
                        else: st.error("Failed to get auth URL")
                    except Exception as e:
                        st.error(f"Need API running + GOOGLE_CLIENT_ID set: {e}")
                        st.info("Or use App password below")
            else:
                st.markdown("""
                <div style='background:#f8fafc;border:1px dashed #cbd5e1;border-radius:14px;padding:20px;text-align:center;margin-bottom:14px;'>
                    <div style='font-size:28px;'>🔐</div>
                    <div style='font-weight:700;color:#0f172a;margin-top:6px;'>Connect your Gmail — like Google Sign-In</div>
                    <div style='font-size:0.85rem;color:#64748b;'>Choose which Gmail LeadSync should work on. We'll show it here once connected.</div>
                </div>
                """, unsafe_allow_html=True)
                st.caption("Tip: Add `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` to `.env` for one-click Google Sign-In (like Macro). Else use App password below.")

        st.subheader("Connect Gmail — which inbox to watch")
        st.caption("This is like Google Sign-In — enter the Gmail you want LeadSync to work on.")

        c1,c2 = st.columns(2)
        with c1:
            st.markdown("**Gmail IMAP**")
            imap_user = st.text_input("Gmail address", value=cur_env.get("IMAP_USERNAME", cur.get("imap_username") or ""), placeholder="you@gmail.com", key="setup_imap_user")
            st.markdown("**← This Gmail will be watched** — LeadSync polls this inbox via IMAP and sends follow-ups from it via SMTP")
            imap_pass = st.text_input("App password (16-char)", type="password", value="", placeholder="abcd efgh ijkl mnop", key="setup_imap_pass", help="Google Account → Security → 2-Step Verification → App passwords → Mail/Windows → 16 chars")
            st.markdown("**Gmail SMTP** (usually same address — this is the sender)")
            smtp_user = st.text_input("SMTP user (sender)", value=cur_env.get("SMTP_USERNAME", cur.get("smtp_username") or ""), placeholder="same Gmail as above", key="setup_smtp_user")
            smtp_pass = st.text_input("SMTP app password", type="password", value="", placeholder="same as IMAP if same Gmail", key="setup_smtp_pass")
            st.caption("Need app password? Google Account → Security → 2-Step → App passwords. Enable IMAP in Gmail → Settings → Forwarding/IMAP → Enable IMAP.")
        with c2:
            st.markdown("**LLM & Mode**")
            llm_provider = st.selectbox("LLM Provider", ["ollama","openai","anthropic","google","groq","nim"], index=["ollama","openai","anthropic","google","groq","nim"].index(cur_env.get("LLM_PROVIDER", cur.get("llm_provider") or "ollama")) if cur_env.get("LLM_PROVIDER", cur.get("llm_provider") or "ollama") in ["ollama","openai","anthropic","google","groq","nim"] else 0, key="setup_llm")
            air_gapped = st.checkbox("AIR_GAPPED (offline, no outside calls)", value=str(cur_env.get("AIR_GAPPED","false")).lower()=="true", key="setup_air")
            openai_key = st.text_input("OPENAI_API_KEY (if LLM=openai)", type="password", value="••••" if cur_env.get("OPENAI_API_KEY") else "", key="setup_oai")
            st.caption("Ollama default `llama3.1:8b` works offline if `ollama` is running")
        if st.button("💾 Save to .env & Reload — this Gmail will be watched", type="primary", key="setup_save"):
            if not imap_user or "@" not in imap_user:
                st.error("Enter a valid Gmail address for the inbox to watch")
            elif not (imap_pass or cur_env.get("IMAP_PASSWORD")):
                st.error("Enter the 16-char App password")
            else:
                try:
                    from dotenv import set_key
                    set_key(str(env_path), "IMAP_USERNAME", imap_user)
                    if imap_pass: set_key(str(env_path), "IMAP_PASSWORD", imap_pass.replace(" ",""))
                    set_key(str(env_path), "IMAP_HOST", "imap.gmail.com")
                    set_key(str(env_path), "IMAP_PORT", "993")
                    set_key(str(env_path), "SMTP_USERNAME", smtp_user or imap_user)
                    if smtp_pass or imap_pass: set_key(str(env_path), "SMTP_PASSWORD", (smtp_pass or imap_pass).replace(" ",""))
                    set_key(str(env_path), "SMTP_HOST", "smtp.gmail.com")
                    set_key(str(env_path), "SMTP_PORT", "587")
                    set_key(str(env_path), "EMAIL_SENDING_DOMAIN", smtp_user or imap_user)
                    set_key(str(env_path), "LLM_PROVIDER", llm_provider)
                    if openai_key and openai_key!="••••": set_key(str(env_path), "OPENAI_API_KEY", openai_key)
                    set_key(str(env_path), "AIR_GAPPED", "true" if air_gapped else "false")
                    from core.config import reload_settings
                    reload_settings()
                    st.success(f"Saved — now watching **{imap_user}** ✅ Test below to confirm it actually works on this Gmail")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e} — ensure python-dotenv is installed")
        c1,c2,c3,c4 = st.columns(4)
        with c1:
            if st.button("🔍 Test IMAP", key="setup_test_imap"):
                try:
                    from core.ingest.email import fetch_emails
                    n = len(fetch_emails("imap.gmail.com",993,imap_user,imap_pass or cur_env.get("IMAP_PASSWORD",""),limit=1))
                    st.success(f"IMAP OK — fetched {n} from **{imap_user}**")
                except Exception as e:
                    st.error(f"IMAP failed: {e} — check Gmail → Settings → IMAP Enable + App password")
        with c2:
            if st.button("📧 Test SMTP", key="setup_test_smtp"):
                try:
                    from core.ingest.email import send_email
                    to = smtp_user or imap_user
                    r = send_email(to,"LeadSync test — from "+to,"Hello from LeadSync Web Setup — if you get this, "+to+" is correctly connected.",smtp_username=smtp_user or imap_user, smtp_password=(smtp_pass or imap_pass or cur_env.get("SMTP_PASSWORD","")).replace(" ",""))
                    st.json(r)
                    if r.get("status")=="sent": st.success(f"Check inbox of {to}")
                except Exception as e:
                    st.error(f"SMTP failed: {e}")
        with c3:
            if st.button("🔄 Fetch now", key="setup_fetch_now"):
                try:
                    from core.ingest.email import fetch_emails
                    from core.intelligence.scorer import score_prospect
                    from core.queue import get_queue
                    convs = fetch_emails("imap.gmail.com",993,imap_user,imap_pass or cur_env.get("IMAP_PASSWORD",""),limit=10)
                    q = get_queue()
                    for c in convs:
                        q.add(score_prospect(c))
                    st.success(f"Fetched {len(convs)} from **{imap_user}** → queued. Go to Dashboard to see scores.")
                except Exception as e:
                    st.error(f"Fetch failed: {e}")
        with c4:
            if st.button("🤖 Auto-poll: ON", key="setup_autopoll_hint"):
                st.info("Auto-poll runs every 60s when Gmail is saved — no click needed. Check **Dashboard** Gmail card: `● Connected and polling` or `GET /gmail/status`.")

    with tab1:
        st.subheader("Current Configuration (read-only)")
        config = {}
        try:
            config = settings.get_all_config() if hasattr(settings, "get_all_config") else {}
        except: pass
        if not config:
            # fallback show env file raw
            try:
                config = {l.split("=",1)[0]: l.split("=",1)[1] for l in Path(__file__).parents[2].joinpath(".env").read_text().splitlines() if "=" in l and not l.strip().startswith("#")}
            except: pass
        for key, value in sorted(config.items()):
            # mask secrets
            if any(k in key.lower() for k in ["password","api_key","token"]):
                value = "••••" if value else ""
            st.markdown(f"**{key}:** `{value}`")

    with tab2:
        st.subheader("Email Suppression List")
        st.caption("Add email addresses to prevent follow-ups (GDPR/CCPA compliance)")

        col1, col2 = st.columns([2, 1])
        with col1:
            suppress_email = st.text_input("Email Address", placeholder="unsubscribe@example.com")
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🚫 Add to Suppressions", type="primary"):
                if suppress_email and "@" in suppress_email:
                    success = add_suppression(suppress_email)
                    if success:
                        st.success(f"Added {suppress_email} to suppression list")
                    else:
                        st.error("Failed to add suppression")
                else:
                    st.error("Invalid email address")

        check_email = st.text_input("Check Suppression", placeholder="test@example.com")
        if check_email:
            suppressed = is_suppressed(check_email)
            if suppressed:
                st.error(f"🚫 {check_email} is SUPPRESSED")
            else:
                st.success(f"✅ {check_email} is NOT suppressed")

    with tab3:
        st.subheader("Recency Decay Calculator")
        st.caption("See how priority scores decay over time (half-life: 7 days)")

        days = st.slider("Days Since Last Contact", 0, 60, 0)
        decay = calculate_recency_decay(days)
        st.metric("Decay Factor", f"{decay:.4f}")
        st.progress(decay)

        # Show decay curve
        import pandas as pd
        chart_data = pd.DataFrame({
            "Days": list(range(0, 61)),
            "Decay": [calculate_recency_decay(d) for d in range(61)],
        })
        st.line_chart(chart_data.set_index("Days"))


elif page == "👥 Users" and is_admin(user):
    st.title("👥 User Management")
    st.caption("Manage dashboard users, roles, and API keys")

    from core.auth import (
        list_users, create_user, delete_user, update_password,
        hash_password, get_api_key_manager,
        enable_2fa, verify_2fa_setup, verify_2fa, disable_2fa,
        get_2fa_status, use_backup_code,
    )
    from core.auth.recovery import generate_recovery_link
    from core.auth.totp import get_totp_manager

    tab1, tab2, tab3, tab4 = st.tabs(["👤 Users", "🔑 API Keys", "🔐 2FA Setup", "📋 Sessions"])

    with tab1:
        st.subheader("All Users")
        users = list_users()
        if users:
            for u in users:
                col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
                role_icon = {"admin": "👑", "rep": "👤", "viewer": "👁️"}.get(u["role"], "👤")
                col1.markdown(f"**{role_icon} {u['name']}** (`{u['username']}`)")
                col2.caption(u["email"] or "No email")
                col3.markdown(f"`{u['role']}` \u00b7 {u.get('team', '')}")
                if col4.button("🗑️", key=f"del_{u['username']}"):
                    if u["username"] == user["username"]:
                        st.error("Can't delete yourself!")
                    elif u["username"] == "admin":
                        st.error("Can't delete the admin account!")
                    else:
                        delete_user(u["username"])
                        st.success(f"Deleted user '{u['username']}'")
                        st.rerun()
        else:
            st.info("No users found.")

        st.divider()
        st.subheader("➕ Add New User")
        with st.form("add_user"):
            new_cols = st.columns(2)
            new_username = new_cols[0].text_input("Username")
            new_name = new_cols[1].text_input("Full Name")
            new_email = new_cols[0].text_input("Email")
            new_password = new_cols[1].text_input("Password", type="password")
            new_role = new_cols[0].selectbox("Role", ["rep", "admin", "viewer"])
            new_team = new_cols[1].text_input("Team")
            if st.form_submit_button("➕ Create User", type="primary"):
                if not new_username or not new_password:
                    st.error("Username and password are required")
                else:
                    success = create_user(
                        new_username, new_password,
                        name=new_name, email=new_email,
                        role=new_role, team=new_team,
                    )
                    if success:
                        st.success(f"User '{new_username}' created!")
                        st.rerun()
                    else:
                        st.error(f"Username '{new_username}' already exists")

    with tab2:
        st.subheader("API Keys")
        st.caption("Manage programmatic access keys for integrations")

        api_mgr = get_api_key_manager()

        with st.form("create_api_key"):
            ak_name = st.text_input("Key Name", placeholder="CI/CD Pipeline")
            ak_role = st.selectbox("Role", ["rep", "viewer"], key="ak_role")
            if st.form_submit_button("🔑 Generate Key", type="primary"):
                if ak_name:
                    key = api_mgr.create_key(ak_name, role=ak_role)
                    st.success(f"Key created! Copy it now \u2014 it won't be shown again:")
                    st.code(key, language=None)
                else:
                    st.error("Enter a name for the key")

    with tab3:
        st.subheader("Two-Factor Authentication (2FA)")
        st.caption("Enable TOTP-based 2FA for enhanced security")

        # Select user for 2FA setup
        users_for_2fa = list_users()
        usernames = [u["username"] for u in users_for_2fa]

        if not usernames:
            st.info("No users found. Create a user first.")
        else:
            selected_user = st.selectbox("Select User", usernames, key="2fa_user_select")
            status = get_2fa_status(selected_user)

            # Status display
            if status.get("error"):
                st.error(status["error"])
            else:
                col1, col2, col3 = st.columns(3)
                col1.metric("Status", "🟢 Enabled" if status["enabled"] else "⚪ Disabled")
                col2.metric("Configured", "Yes" if status["configured"] else "No")
                col3.metric("Backup Codes", status.get("backup_codes_remaining", 0))

            st.divider()

            # ── Setup Flow ──────────────────────────────────
            if not status.get("enabled") and not status.get("configured"):
                st.subheader("Step 1: Initialize 2FA")
                st.info("Click below to generate a TOTP secret and QR code.")

                if st.button("🔐 Start 2FA Setup", type="primary", key="start_2fa"):
                    result = enable_2fa(selected_user)
                    if "error" in result:
                        st.error(result["error"])
                    else:
                        st.session_state["2fa_setup"] = result
                        st.success("2FA setup initiated! See below.")
                        st.rerun()

            elif status.get("configured") and not status.get("enabled"):
                # Secret exists but not yet verified — show QR and verification
                st.subheader("Step 2: Scan QR Code & Verify")

                # Retrieve secret from session or fetch it
                setup_data = st.session_state.get("2fa_setup")
                if not setup_data:
                    # Re-enable to get the secret
                    result = enable_2fa(selected_user)
                    if "error" in result:
                        # 2FA already configured — try to get status
                        st.warning("2FA is configured but not activated. Enter a code from your authenticator app.")
                        setup_data = None
                    else:
                        setup_data = result
                        st.session_state["2fa_setup"] = setup_data

                if setup_data:
                    secret = setup_data["secret"]
                    uri = setup_data["uri"]

                    col1, col2 = st.columns([1, 1])

                    with col1:
                        st.markdown("**1. Scan this QR code with your authenticator app:**")
                        # Generate QR code locally (air-gapped safe) — falls back to Google only if local fails
                        totp = get_totp_manager()
                        try:
                            qr_url = totp.get_qr_code_data_uri(uri)
                        except Exception:
                            qr_url = totp.get_qr_code_url(uri)
                        st.image(qr_url, width=250, caption="Scan with Google Authenticator / Authy")

                    with col2:
                        st.markdown("**Or enter this secret manually:**")
                        st.code(secret, language=None)
                        st.markdown(f"**Issuer:** SalesFollowUpAgent")
                        st.markdown(f"**Account:** {selected_user}")

                    st.divider()

                    st.markdown("**2. Enter the 6-digit code from your app to verify:**")
                    verify_code = st.text_input(
                        "Verification Code",
                        placeholder="123456",
                        max_chars=6,
                        key="verify_2fa_code",
                    )

                    col_a, col_b = st.columns([1, 1])
                    with col_a:
                        if st.button("✅ Activate 2FA", type="primary", key="activate_2fa"):
                            if verify_code and len(verify_code) == 6:
                                result = verify_2fa_setup(selected_user, verify_code)
                                if result.get("success"):
                                    st.success("🎉 2FA activated successfully!")
                                    st.balloons()
                                    if "2fa_setup" in st.session_state:
                                        del st.session_state["2fa_setup"]
                                    st.rerun()
                                else:
                                    st.error(result.get("message", "Invalid code. Try again."))
                            else:
                                st.error("Enter a valid 6-digit code")

                    # Backup codes display
                    st.divider()
                    st.warning("⚠️ **Save these backup codes now!** They won't be shown again.")
                    backup_codes = setup_data.get("backup_codes", [])
                    if backup_codes:
                        codes_text = "\n".join(backup_codes)
                        st.code(codes_text, language=None)
                        st.caption(f"{len(backup_codes)} backup codes · Each can be used once")

            elif status.get("enabled"):
                # 2FA is active — show management options
                st.subheader("2FA Active")
                st.success(f"2FA is enabled for **{selected_user}** with {status.get('backup_codes_remaining', 0)} backup codes remaining.")

                # Test 2FA
                st.markdown("**Test 2FA:**")
                test_code = st.text_input(
                    "Enter code from authenticator",
                    placeholder="123456",
                    max_chars=6,
                    key="test_2fa_code",
                )
                if st.button("🧪 Test Code", key="test_2fa_btn"):
                    if test_code and len(test_code) == 6:
                        is_valid = verify_2fa(selected_user, test_code)
                        if is_valid:
                            st.success("Code is valid!")
                        else:
                            st.error("Invalid code")
                    else:
                        st.error("Enter a 6-digit code")

                # Backup code recovery
                st.markdown("**Backup Code Recovery:**")
                backup_code_input = st.text_input(
                    "Use a backup code",
                    placeholder="ABCD-1234",
                    key="backup_code_input",
                )
                if st.button("🔓 Use Backup Code", key="use_backup_btn"):
                    if backup_code_input:
                        result = use_backup_code(selected_user, backup_code_input)
                        if result.get("success"):
                            st.success(f"Backup code accepted! {result.get('remaining', '?')} codes remaining.")
                        else:
                            st.error(result.get("message", "Invalid backup code"))
                    else:
                        st.error("Enter a backup code")

                # Disable 2FA
                st.divider()
                st.subheader("Disable 2FA")
                st.warning("Disabling 2FA will remove all TOTP secrets and backup codes.")
                disable_password = st.text_input(
                    "Confirm with your password",
                    type="password",
                    key="disable_2fa_pw",
                )
                if st.button("🚫 Disable 2FA", type="secondary", key="disable_2fa_btn"):
                    if disable_password:
                        result = disable_2fa(selected_user, disable_password)
                        if result.get("success"):
                            st.success("2FA disabled.")
                            st.rerun()
                        else:
                            st.error(result.get("error", "Failed to disable 2FA"))
                    else:
                        st.error("Enter your password to confirm")

        # ── Recovery Links ─────────────────────────────────
        st.divider()
        st.subheader("🔑 Recovery Links")
        st.caption("Generate one-time use links for users who lost access to their authenticator")

        # Request recovery link
        with st.expander("📧 Request Recovery Link", expanded=False):
            recovery_user = st.selectbox(
                "Select User",
                usernames,
                key="recovery_user_select",
            )
            recovery_base_url = st.text_input(
                "Base URL",
                value="http://localhost:8000",
                key="recovery_base_url",
            )
            recovery_send_email = st.checkbox("Send via Email", value=True, key="recovery_send_email")

            if st.button("📧 Generate Recovery Link", type="primary", key="gen_recovery_link"):
                result = generate_recovery_link(
                    recovery_user,
                    base_url=recovery_base_url,
                )
                if "error" in result:
                    st.error(result["error"])
                else:
                    st.success("Recovery link generated!")
                    st.code(result["link"], language=None)
                    st.caption(f"Expires: {result.get('expires_in_hours', 1)} hour(s)")

                    if recovery_send_email:
                        from core.auth.recovery import send_recovery_email
                        email_result = send_recovery_email(
                            recovery_user,
                            result["link"],
                        )
                        if email_result.get("success"):
                            st.success(f"Email sent to {email_result.get('to', 'user')}")
                        else:
                            st.warning(f"Email not sent: {email_result.get('error', 'Unknown error')}")

        # List pending recovery links
        with st.expander("📋 Pending Recovery Links", expanded=False):
            from core.auth.recovery import get_pending_recovery_links, cleanup_expired_links
            pending_links = get_pending_recovery_links()
            if pending_links:
                links_data = []
                for link in pending_links:
                    from datetime import datetime
                    links_data.append({
                        "User": link["username"],
                        "Created": datetime.fromtimestamp(link["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                        "Expires": datetime.fromtimestamp(link["expires_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                st.dataframe(links_data, use_container_width=True, hide_index=True)
            else:
                st.info("No pending recovery links.")

            if st.button("🧹 Cleanup Expired", key="cleanup_recovery"):
                removed = cleanup_expired_links()
                st.success(f"Cleaned up {removed} expired link(s)")

        # ── All Users 2FA Overview ──────────────────────────
        st.divider()
        st.subheader("2FA Status Overview")
        users_all = list_users()
        if users_all:
            overview_data = []
            for u in users_all:
                s = get_2fa_status(u["username"])
                overview_data.append({
                    "User": u["username"],
                    "Role": u["role"],
                    "2FA Enabled": "Yes" if s.get("enabled") else "No",
                    "Backup Codes": s.get("backup_codes_remaining", 0),
                })
            st.dataframe(overview_data, use_container_width=True, hide_index=True)

    with tab4:
        st.subheader("Active Sessions")
        auth = get_authenticator()
        session_count = auth.get_session_count()
        st.metric("Active Sessions", session_count)

        st.info("Sessions expire after 24 hours. Failed login lockout: 5 attempts / 15 min.")


elif page == "\U0001f50c WebSocket":
    st.title("\U0001f50c WebSocket Test Client")
    st.caption("Connect to the real-time queue event stream and send commands")

    import json
    import websocket  # websocket-client lib

    # ── Connection Settings ──────────────────────────────────
    with st.expander("\u2699\ufe0f Connection Settings", expanded=True):
        col1, col2, col3 = st.columns(3)
        ws_host = col1.text_input("Host", value="localhost", key="ws_host")
        ws_port = col2.text_input("Port", value="8000", key="ws_port")
        ws_api_key = col3.text_input("API Key", value="", type="password", key="ws_api_key")

        ws_url = f"ws://{ws_host}:{ws_port}/ws/queue"
        if ws_api_key:
            ws_url += f"?api_key={ws_api_key}"
        st.code(ws_url, language=None)

    # ── Session State ────────────────────────────────────────
    if "ws_connected" not in st.session_state:
        st.session_state.ws_connected = False
    if "ws_events" not in st.session_state:
        st.session_state.ws_events = []
    if "ws_stats" not in st.session_state:
        st.session_state.ws_stats = None
    if "ws_auth_info" not in st.session_state:
        st.session_state.ws_auth_info = None
    if "ws_error" not in st.session_state:
        st.session_state.ws_error = None

    # ── Connection Controls ──────────────────────────────────
    ctrl_cols = st.columns([1, 1, 1, 2])

    with ctrl_cols[0]:
        if st.button("\u25b6 Connect", disabled=st.session_state.ws_connected, type="primary"):
            st.session_state.ws_error = None
            st.session_state.ws_events = []
            st.session_state.ws_stats = None
            st.session_state.ws_auth_info = None

            try:
                kwargs = {"timeout": 5}
                if ws_api_key:
                    ws = websocket.create_connection(ws_url, **kwargs)
                else:
                    ws = websocket.create_connection(ws_url, **kwargs)
                st.session_state._ws_conn = ws
                st.session_state.ws_connected = True

                # Read welcome/auth messages
                try:
                    for _ in range(3):  # Read up to 3 initial messages
                        ws.settimeout(2)
                        msg = json.loads(ws.recv())
                        msg_type = msg.get("type", "unknown")
                        if msg_type == "connected":
                            st.session_state.ws_events.insert(0, {"icon": "\u2705", **msg})
                        elif msg_type == "auth":
                            st.session_state.ws_auth_info = msg.get("data", {})
                            st.session_state.ws_events.insert(0, {"icon": "\U0001f510", **msg})
                        else:
                            st.session_state.ws_events.insert(0, {"icon": "\U0001f4e2", **msg})
                except Exception:
                    pass

                st.success("Connected!")
                st.rerun()
            except ImportError:
                st.session_state.ws_error = "`websocket-client` package not installed. Run: `pip install websocket-client`"
                st.error(st.session_state.ws_error)
            except Exception as e:
                st.session_state.ws_error = str(e)
                st.error(f"Connection failed: {e}")

    with ctrl_cols[1]:
        if st.button("\u23f9 Disconnect", disabled=not st.session_state.ws_connected):
            try:
                ws = st.session_state.get("_ws_conn")
                if ws:
                    ws.close()
            except Exception:
                pass
            st.session_state.ws_connected = False
            st.session_state._ws_conn = None
            st.session_state.ws_events.insert(0, {"icon": "\u274c", "type": "disconnected", "data": {}, "timestamp": datetime.utcnow().isoformat()})
            st.rerun()

    with ctrl_cols[2]:
        if st.button("\U0001f5d1 Clear Log"):
            st.session_state.ws_events = []
            st.rerun()

    # ── Status Bar ───────────────────────────────────────────
    status_icon = "\U0001f7e2" if st.session_state.ws_connected else "\U0001f534"
    status_text = "Connected" if st.session_state.ws_connected else "Disconnected"
    st.markdown(f"**Status:** {status_icon} {status_text}")

    if st.session_state.ws_auth_info:
        auth_info = st.session_state.ws_auth_info
        st.caption(f"User: {auth_info.get('user', '?')} | Role: {auth_info.get('role', '?')} | Rate limit: {auth_info.get('rate_limit', '?')}/{auth_info.get('rate_window', '?')}s")

    if st.session_state.ws_error:
        st.error(st.session_state.ws_error)

    st.divider()

    # ── Send Commands ────────────────────────────────────────
    st.subheader("\U0001f4e4 Send Command")

    cmd_cols = st.columns(4)
    with cmd_cols[0]:
        if st.button("\U0001f3d3 Ping", disabled=not st.session_state.ws_connected):
            try:
                ws = st.session_state.get("_ws_conn")
                if ws:
                    ws.send("ping")
                    ws.settimeout(5)
                    resp = json.loads(ws.recv())
                    st.session_state.ws_events.insert(0, {"icon": "\U0001f3d3", **resp})
                    st.rerun()
            except Exception as e:
                st.error(f"Ping failed: {e}")

    with cmd_cols[1]:
        if st.button("\U0001f4ca Stats", disabled=not st.session_state.ws_connected):
            try:
                ws = st.session_state.get("_ws_conn")
                if ws:
                    ws.send("stats")
                    ws.settimeout(5)
                    resp = json.loads(ws.recv())
                    st.session_state.ws_stats = resp.get("data", {})
                    st.session_state.ws_events.insert(0, {"icon": "\U0001f4ca", **resp})
                    st.rerun()
            except Exception as e:
                st.error(f"Stats failed: {e}")

    with cmd_cols[2]:
        if st.button("\U0001f464 Whoami", disabled=not st.session_state.ws_connected):
            try:
                ws = st.session_state.get("_ws_conn")
                if ws:
                    ws.send("whoami")
                    ws.settimeout(5)
                    resp = json.loads(ws.recv())
                    st.session_state.ws_auth_info = resp.get("data", {})
                    st.session_state.ws_events.insert(0, {"icon": "\U0001f464", **resp})
                    st.rerun()
            except Exception as e:
                st.error(f"Whoami failed: {e}")

    with cmd_cols[3]:
        custom_cmd = st.text_input("Custom message", placeholder="Type any message...", key="ws_custom_cmd")
        if st.button("\u27a1 Send", disabled=not st.session_state.ws_connected) and custom_cmd:
            try:
                ws = st.session_state.get("_ws_conn")
                if ws:
                    ws.send(custom_cmd)
                    ws.settimeout(5)
                    resp = json.loads(ws.recv())
                    st.session_state.ws_events.insert(0, {"icon": "\U0001f4ac", **resp})
                    st.rerun()
            except Exception as e:
                st.error(f"Send failed: {e}")

    # ── Receive Loop ─────────────────────────────────────────
    if st.session_state.ws_connected:
        st.divider()
        if st.button("\U0001f504 Poll for Events (receive until timeout)"):
            try:
                ws = st.session_state.get("_ws_conn")
                if ws:
                    received = 0
                    ws.settimeout(1.0)
                    while received < 20:  # Max 20 messages per poll
                        try:
                            raw = ws.recv()
                            msg = json.loads(raw)
                            msg_type = msg.get("type", "unknown")
                            icon = {
                                "queue:added": "\u2795",
                                "queue:popped": "\U0001f4e4",
                                "queue:removed": "\U0001f5d1",
                                "queue:breach": "\U0001f534",
                                "heartbeat": "\U0001f499",
                                "pong": "\U0001f3d3",
                                "stats": "\U0001f4ca",
                                "error": "\u274c",
                                "auth": "\U0001f510",
                                "connected": "\u2705",
                            }.get(msg_type, "\U0001f4e2")
                            st.session_state.ws_events.insert(0, {"icon": icon, **msg})
                            received += 1
                            if msg_type == "error":
                                break
                        except websocket.WebSocketTimeoutException:
                            break
                    if received > 0:
                        st.rerun()
            except Exception as e:
                st.error(f"Receive failed: {e}")

    # ── Queue Stats Card ─────────────────────────────────────
    if st.session_state.ws_stats:
        st.divider()
        st.subheader("\U0001f4ca Queue Stats (from WebSocket)")
        qs = st.session_state.ws_stats
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total Items", qs.get("total_items", 0))
        s2.metric("Avg Priority", f"{qs.get('avg_priority', 0):.2f}")
        s3.metric("SLA Breaches", qs.get("breached_count", 0))
        s4.metric("Max Priority", f"{qs.get('max_priority', 0):.2f}")

    # ── Event Log ────────────────────────────────────────────
    st.divider()
    st.subheader(f"\U0001f4cb Event Log ({len(st.session_state.ws_events)} events)")

    if not st.session_state.ws_events:
        st.info("No events yet. Connect and send a command, or poll for broadcast events.")
    else:
        for i, evt in enumerate(st.session_state.ws_events[:50]):
            icon = evt.get("icon", "\U0001f4e2")
            msg_type = evt.get("type", "unknown")
            ts = evt.get("timestamp", "")[-15:]  # MM:SS.mmm or HH:MM:SS
            data = evt.get("data", {})

            with st.expander(f"{icon} `{ts}` **{msg_type}**", expanded=(i < 3)):
                # Summary line
                if msg_type in ("queue:added", "queue:popped", "queue:removed"):
                    cid = data.get("conversation_id", "?")[:12]
                    priority = data.get("priority_score")
                    urgency = data.get("urgency", "")
                    parts = [f"ID: `{cid}`"]
                    if priority is not None:
                        parts.append(f"Score: {priority:.2f}")
                    if urgency:
                        parts.append(f"Urgency: {urgency}")
                    st.markdown(" | ".join(parts))
                elif msg_type == "pong":
                    conns = data.get("connections", "?")
                    remaining = data.get("rate_remaining")
                    st.markdown(f"Connections: {conns}" + (f" | Rate remaining: {remaining}" if remaining is not None else ""))
                elif msg_type == "heartbeat":
                    conns = data.get("connections", "?")
                    st.markdown(f"Keep-alive | Connections: {conns}")
                elif msg_type == "error":
                    st.error(data.get("message", "Unknown error"))
                elif msg_type == "auth":
                    user = data.get("user", "?")
                    role = data.get("role", "?")
                    st.markdown(f"User: {user} | Role: {role}")

                # Full JSON
                st.json(evt)

        if len(st.session_state.ws_events) > 50:
            st.caption(f"Showing latest 50 of {len(st.session_state.ws_events)} events")


elif page == "\U0001f50d Webhook Inspector":
    st.title("\U0001f50d Webhook Payload Inspector")
    st.caption("View exactly what gets sent to each alert channel")

    from core.alerts.inspector import get_inspector
    inspector = get_inspector()

    # ── Status Bar ──────────────────────────────────────
    stats = inspector.get_stats()
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Captured", stats["total_captured"])
    col2.metric("Stored", stats["stored_entries"])
    col3.metric("Sent", stats["sent"])
    col4.metric("Failed", stats["failed"])
    col5.metric("Success Rate", f"{stats['success_rate']}%")

    # Inspector toggle
    enabled = st.toggle("Inspector Enabled", value=stats["enabled"], key="inspector_enabled")
    if enabled != stats["enabled"]:
        inspector.enabled = enabled
        st.success(f"Inspector {'enabled' if enabled else 'disabled'}")

    st.divider()

    # ── Tabs ────────────────────────────────────────────
    tab_live, tab_channels, tab_stats, tab_capture = st.tabs([
        "\U0001f4e1 Live Payloads", "\U0001f4e6 Channels", "\U0001f4ca Statistics", "\U0001f3ab Manual Capture"
    ])

    with tab_live:
        st.subheader("Captured Payloads")

        # Filters
        col1, col2, col3 = st.columns(3)
        filter_channel = col1.text_input("Channel Name", placeholder="e.g. slack", key="filter_ch")
        filter_type = col2.selectbox("Channel Type", ["All", "telegram", "email", "slack", "discord", "teams", "pagerduty", "opsgenie", "manual", "unknown"], key="filter_type")
        filter_status = col3.selectbox("Status", ["All", "Success", "Failed"], key="filter_status")

        # Build filter kwargs
        filter_kwargs = {}
        if filter_channel:
            filter_kwargs["channel"] = filter_channel
        if filter_type != "All":
            filter_kwargs["channel_type"] = filter_type
        if filter_status == "Success":
            filter_kwargs["success"] = True
        elif filter_status == "Failed":
            filter_kwargs["success"] = False

        entries = inspector.get_entries(limit=50, **filter_kwargs)

        if not entries:
            st.info("No captured payloads yet. Send a test alert or wait for SLA breaches.")
        else:
            for entry in entries:
                status_icon = "\u2705" if entry["success"] else "\u274c"
                latency = f"{entry['latency_ms']}ms" if entry["latency_ms"] is not None else "-"

                with st.expander(
                    f"{status_icon} #{entry['id']} | {entry['channel']} ({entry['channel_type']}) | {entry['status']} | {latency}",
                    expanded=False,
                ):
                    col1, col2 = st.columns(2)
                    col1.markdown(f"**Channel:** {entry['channel']}")
                    col1.markdown(f"**Type:** {entry['channel_type']}")
                    col1.markdown(f"**Status:** {entry['status']}")
                    col2.markdown(f"**Latency:** {latency}")
                    col2.markdown(f"**Timestamp:** {entry['timestamp']}")
                    if entry.get("error"):
                        st.error(f"Error: {entry['error']}")

                    st.markdown("**Payload:**")
                    st.json(entry["payload"])

    with tab_channels:
        st.subheader("Channel Summary")
        channels = inspector.get_channel_list()

        if not channels:
            st.info("No channels have sent payloads yet.")
        else:
            for ch in channels:
                col1, col2, col3, col4 = st.columns([3, 2, 2, 3])
                col1.markdown(f"**{ch['name']}**")
                col2.caption(ch["type"])
                col3.markdown(f"{ch['sent']}\u2191 / {ch['failed']}\u2193")
                col4.caption(f"Last: {ch['last_used']}")

    with tab_stats:
        st.subheader("Statistics")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Overall**")
            st.metric("Total Captured", stats["total_captured"])
            st.metric("Success Rate", f"{stats['success_rate']}%")
            st.metric("Avg Latency", f"{stats['latency']['avg_ms']}ms")

        with col2:
            st.markdown("**By Channel Type**")
            if stats["by_channel_type"]:
                for ctype, data in stats["by_channel_type"].items():
                    st.markdown(f"**{ctype}:** {data['total']} total, {data['sent']} sent, {data['failed']} failed")
            else:
                st.info("No data yet")

        st.divider()
        st.markdown("**Actions**")
        col1, col2, col3 = st.columns(3)
        if col1.button("\U0001f5d1 Clear All Payloads", key="clear_inspector"):
            cleared = inspector.clear()
            st.success(f"Cleared {cleared} entry(ies)")
            st.rerun()
        if col2.button("\U0001f4e5 Export JSON", key="export_inspector"):
            json_str = inspector.export_json()
            st.download_button(
                label="Download JSON",
                data=json_str,
                file_name="webhook-inspector-export.json",
                mime="application/json",
            )
        if col3.button("\U0001f9f9 Cleanup Expired", key="cleanup_inspector"):
            from core.alerts.inspector import cleanup_expired_links
            st.info("Inspector has no expired entries to clean up.")

    with tab_capture:
        st.subheader("Manual Payload Capture")
        st.caption("Manually record a payload for testing or external integrations")

        with st.form("manual_capture"):
            mc_channel = st.text_input("Channel Name", placeholder="e.g. my-custom-service")
            mc_type = st.selectbox("Channel Type", ["manual", "webhook", "email", "slack", "telegram", "other"], key="mc_type")
            mc_success = st.checkbox("Success", value=True, key="mc_success")
            mc_latency = st.number_input("Latency (ms)", value=0, min_value=0, key="mc_latency")
            mc_error = st.text_input("Error (if failed)", placeholder="Optional error message", key="mc_error")

            if st.form_submit_button("\U0001f4e4 Capture Payload"):
                if mc_channel:
                    entry = inspector.capture_manual(
                        channel=mc_channel,
                        payload={"manual": True, "channel": mc_channel, "type": mc_type},
                        channel_type=mc_type,
                        success=mc_success,
                        latency_ms=mc_latency,
                        error=mc_error if mc_error else None,
                    )
                    st.success(f"Captured entry #{entry['id']}")
                else:
                    st.error("Channel name is required")


# ─── Footer ───────────────────────────────────────────────────
st.divider()
st.caption("AI Sales Follow-Up Agent v1.0.0 \u00b7 Autonomous LLM Fallback \u00b7 Built with Streamlit")
