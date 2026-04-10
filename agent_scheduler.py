import schedule
import time
import subprocess
import datetime

def run_agent(name, script):
    print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] Running {name}...")
    try:
        subprocess.run(["python3", script], timeout=300)
        print(f"  {name} completed.")
    except Exception as e:
        print(f"  {name} failed: {e}")

def run_researcher():
    run_agent("Market Research Agent", "agent_researcher.py")

def run_risk():
    run_agent("Risk Monitor Agent", "agent_risk.py")

# Schedule agents
schedule.every(6).hours.do(run_researcher)    # Research every 6 hours
schedule.every(30).minutes.do(run_risk)       # Risk check every 30 min

# Run all agents immediately on startup
print("Agent Scheduler starting...")
print("Running initial checks...")
run_researcher()
run_risk()

print("\nSchedule:")
print("  Market Research: every 6 hours")
print("  Risk Monitor: every 30 minutes")
print("\nScheduler running. Press Ctrl+C to stop.")

while True:
    schedule.run_pending()
    time.sleep(60)
