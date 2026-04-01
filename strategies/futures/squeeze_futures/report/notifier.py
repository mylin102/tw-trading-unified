import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

def send_email_notification(subject: str, body_text: str, body_html: str = None):
    """
    發送 Email 通知。
    支援純文字 (body_text) 與 HTML (body_html) 格式。
    """
    env_path = os.path.expanduser("~/.config/squeeze-backtest-email.env")
    if not os.path.exists(env_path):
        logger.error(f"Email config not found at {env_path}")
        return False
        
    load_dotenv(env_path)
    
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    recipient = os.getenv("SMTP_RECIPIENT")
    
    if not all([username, password, recipient]):
        logger.error("Missing SMTP credentials in config file.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg['From'] = username
        msg['To'] = recipient
        msg['Subject'] = subject
        
        # 加入純文字備案
        msg.attach(MIMEText(body_text, 'plain'))
        
        # 加入 HTML 內文
        if body_html:
            msg.attach(MIMEText(body_html, 'html'))
        
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(username, password)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"Email sent successfully to {recipient}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        return False
