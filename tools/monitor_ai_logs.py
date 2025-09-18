#!/usr/bin/env python3
"""
Monitor CrewAI and LiteLLM logs in real-time.
This script helps debug AI model interactions and CrewAI workflows.
"""

import os
import sys
import time
import subprocess
from pathlib import Path
from threading import Thread
import argparse


def tail_log_file(log_file: Path, prefix: str):
    """Tail a log file and print with prefix."""
    if not log_file.exists():
        print(f"⚠️  {prefix}: Log file {log_file} doesn't exist yet. Waiting...")
        # Wait for file to be created
        while not log_file.exists():
            time.sleep(1)
        print(f"✅ {prefix}: Log file {log_file} created!")
    
    try:
        # Use tail -f to follow the log file
        process = subprocess.Popen(
            ['tail', '-f', str(log_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        for line in iter(process.stdout.readline, ''):
            if line.strip():
                print(f"{prefix}: {line.strip()}")
                
    except KeyboardInterrupt:
        process.terminate()
    except Exception as e:
        print(f"❌ {prefix}: Error tailing {log_file}: {e}")


def monitor_logs(log_dir: Path, logs_to_monitor: list):
    """Monitor multiple log files simultaneously."""
    print(f"🔍 Monitoring AI logs in: {log_dir}")
    print(f"📋 Logs to monitor: {', '.join(logs_to_monitor)}")
    print("=" * 60)
    
    threads = []
    
    for log_name in logs_to_monitor:
        log_file = log_dir / f"{log_name}.log"
        prefix = f"[{log_name.upper()}]"
        
        thread = Thread(target=tail_log_file, args=(log_file, prefix))
        thread.daemon = True
        thread.start()
        threads.append(thread)
    
    try:
        # Keep main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Stopping log monitoring...")
        return


def show_log_status(log_dir: Path):
    """Show status of all AI-related log files."""
    print(f"📊 AI Log Files Status in: {log_dir}")
    print("=" * 60)
    
    ai_logs = [
        "crewai",
        "litellm", 
        "langchain",
        "openai",
        "http_requests",
        "healing_all",
        "healing_operations"
    ]
    
    for log_name in ai_logs:
        log_file = log_dir / f"{log_name}.log"
        if log_file.exists():
            size = log_file.stat().st_size
            size_mb = size / (1024 * 1024)
            mtime = log_file.stat().st_mtime
            mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
            print(f"✅ {log_name:15} - {size_mb:6.2f} MB - Last modified: {mtime_str}")
        else:
            print(f"❌ {log_name:15} - File doesn't exist")


def show_recent_entries(log_dir: Path, log_name: str, lines: int = 20):
    """Show recent entries from a specific log file."""
    log_file = log_dir / f"{log_name}.log"
    
    if not log_file.exists():
        print(f"❌ Log file {log_file} doesn't exist")
        return
    
    print(f"📋 Last {lines} entries from {log_name}.log:")
    print("=" * 60)
    
    try:
        result = subprocess.run(
            ['tail', '-n', str(lines), str(log_file)],
            capture_output=True,
            text=True
        )
        
        if result.stdout:
            print(result.stdout)
        else:
            print("(No recent entries)")
            
    except Exception as e:
        print(f"❌ Error reading {log_file}: {e}")


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Monitor CrewAI and LiteLLM logs")
    parser.add_argument(
        "--log-dir", 
        default="logs", 
        help="Directory containing log files (default: logs)"
    )
    parser.add_argument(
        "--monitor", 
        nargs="+", 
        default=["crewai", "litellm", "langchain", "openai"],
        help="Logs to monitor (default: crewai litellm langchain openai)"
    )
    parser.add_argument(
        "--status", 
        action="store_true",
        help="Show log file status and exit"
    )
    parser.add_argument(
        "--recent", 
        help="Show recent entries from specific log (e.g., --recent crewai)"
    )
    parser.add_argument(
        "--lines", 
        type=int, 
        default=20,
        help="Number of recent lines to show (default: 20)"
    )
    
    args = parser.parse_args()
    
    log_dir = Path(args.log_dir)
    
    if not log_dir.exists():
        print(f"❌ Log directory {log_dir} doesn't exist")
        print("Make sure to run the application first to create log files")
        sys.exit(1)
    
    if args.status:
        show_log_status(log_dir)
        return
    
    if args.recent:
        show_recent_entries(log_dir, args.recent, args.lines)
        return
    
    # Default: monitor logs in real-time
    print("🤖 CrewAI & LiteLLM Log Monitor")
    print("=" * 60)
    print("This tool monitors AI-related logs in real-time")
    print("Press Ctrl+C to stop monitoring")
    print()
    
    monitor_logs(log_dir, args.monitor)


if __name__ == "__main__":
    main()