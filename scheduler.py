import time
from datetime import datetime

import config
from main import run_tariff_compare 
from main import send_message

# Track last execution date to ensure we only run once per day
last_execution_date = None

if config.ONE_OFF_RUN:
    send_message(f"Welcome to Octopus MinMax Bot. Executing a one off comparison.")
    run_tariff_compare()
else:
    send_message(f"Welcome to Octopus MinMax Bot. I will run your comparisons at {config.EXECUTION_TIME}")

    while True:
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        current_date = now.date()

        if current_time == config.EXECUTION_TIME and last_execution_date != current_date:
            send_message(f"Executing tariff comparison at {current_time}...")
            last_execution_date = current_date
            run_tariff_compare()

        time.sleep(30)  # Check time every 30 seconds
