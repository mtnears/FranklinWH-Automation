#!/usr/bin/env python3
"""
Smart Monitor Milestone Emailer
Sends status emails at key times during the day for testing/monitoring

This script is useful during initial setup and testing to monitor system
behavior at critical decision points throughout the day. Can be disabled
once confident in system operation.

Configuration loaded from .env file via config module.
"""
import asyncio
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from franklinwh import Client, TokenFetcher

# Import configuration
from config import config

# Milestone times (hours of day to send status emails)
# Adjust based on your peak period and testing needs
MILESTONES = [8, 10, 12, 14, 16]


def get_recent_log_entries(lines=30):
    """Get recent entries from intelligence log"""
    try:
        with open(config.INTELLIGENCE_LOG, 'r') as f:
            all_lines = f.readlines()
            return ''.join(all_lines[-lines:])
    except Exception as e:
        return f"Could not read log: {e}"


async def get_current_status():
    """Get current battery status"""
    try:
        fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
        client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)
        stats = await client.get_stats()

        return {
            'soc': stats.current.battery_soc,
            'solar': stats.current.solar_production,
            'grid': stats.current.grid_use,
            'battery': stats.current.battery_use,
            'home': stats.current.home_load
        }
    except Exception as e:
        return None


def send_milestone_email(hour, status, log_excerpt):
    """Send milestone status email"""
    
    # Check if email is configured
    if not config.EMAIL_ENABLED:
        print("Email not configured. Set EMAIL_ENABLED=true and configure SMTP settings in .env")
        return False
    
    now = datetime.now()

    subject = f"Solar Intelligence Milestone - {hour}:00 Status"

    if status:
        status_text = f"""
CURRENT STATUS ({now.strftime('%I:%M %p')}):
================================
Battery SOC:      {status['soc']:.1f}%
Solar Production: {status['solar']:.3f} kW
Grid Use:         {status['grid']:.3f} kW
Battery Use:      {status['battery']:.3f} kW
Home Load:        {status['home']:.3f} kW
"""
    else:
        status_text = "Could not retrieve current status"

    body = f"""
Smart Battery Monitor - {hour}:00 Milestone Report
========================================================

{status_text}

RECENT INTELLIGENCE DECISIONS:
================================
{log_excerpt}

========================================================
This is an automated milestone report for testing.
Full logs: {config.LOG_DIR}
"""

    try:
        msg = MIMEMultipart()
        msg['From'] = config.SMTP_SENDER
        msg['To'] = config.SMTP_RECIPIENT
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_SENDER, config.SMTP_PASSWORD)
            server.send_message(msg)

        print(f"✓ Milestone email sent for {hour}:00")
        return True
    except Exception as e:
        print(f"✗ Failed to send email: {e}")
        return False


async def check_and_send_milestone():
    """Check if we're at a milestone hour and send email"""
    now = datetime.now()
    current_hour = now.hour

    if current_hour in MILESTONES:
        # Check if we already sent for this hour (within last 10 minutes)
        minutes = now.minute
        if minutes < 10:  # Only send in first 10 minutes of the hour
            print(f"Milestone hour {current_hour}:00 - sending email")
            status = await get_current_status()
            log_excerpt = get_recent_log_entries(30)
            send_milestone_email(current_hour, status, log_excerpt)
        else:
            print(f"Milestone hour {current_hour}:00 but already past window")
    else:
        print(f"Not a milestone hour (current: {current_hour}:00)")


if __name__ == "__main__":
    asyncio.run(check_and_send_milestone())
