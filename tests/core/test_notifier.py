# 2026-06-26 Gemini CLI: unit tests for email notification toggle
import os
import unittest
from unittest.mock import patch, MagicMock

from core.notification.notifier import _send_email

class TestNotifierToggle(unittest.TestCase):
    def setUp(self):
        # Backup environment variables
        self.original_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)

    @patch('core.notification.notifier._load_smtp_config')
    def test_send_email_disabled_toggle(self, mock_load_smtp):
        # Set the toggle to disable email notification
        os.environ["ENABLE_EMAIL_NOTIFICATION"] = "False"
        
        # Call _send_email
        res = _send_email("Test Subject", "Test Body")
        
        # Assert return True (indicating skip/success)
        self.assertTrue(res)
        
        # Verify that _load_smtp_config was called (which loads the .env variables)
        mock_load_smtp.assert_called_once()

    @patch('core.notification.notifier._load_smtp_config')
    @patch('smtplib.SMTP')
    def test_send_email_enabled_toggle(self, mock_smtp, mock_load_smtp):
        # Set the toggle to True
        os.environ["ENABLE_EMAIL_NOTIFICATION"] = "True"
        
        # Configure SMTP mock config
        mock_load_smtp.return_value = {
            "server": "smtp.example.com",
            "port": 587,
            "username": "sender@example.com",
            "password": "password",
            "recipient": "recipient@example.com",
        }
        
        # Mock SMTP context manager
        mock_smtp_inst = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_smtp_inst
        
        # Call _send_email
        res = _send_email("Test Subject", "Test Body")
        
        # Assert email was sent successfully
        self.assertTrue(res)
        mock_smtp.assert_called_once_with("smtp.example.com", 587)
        mock_smtp_inst.starttls.assert_called_once()
        mock_smtp_inst.login.assert_called_once_with("sender@example.com", "password")
        mock_smtp_inst.send_message.assert_called_once()
