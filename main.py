import os
import requests
import time
import traceback
from datetime import datetime, date
from playwright.sync_api import sync_playwright
from gql import gql, Client
from gql.transport.aiohttp import AIOHTTPTransport
import config

# Global variables for GraphQL client
gql_transport = None
gql_client = None

# List of potential tariffs to compare
potential_tariffs = ["AGILE", "GO", "COSY"]

# GraphQL queries and mutations
token_query = """mutation {{
    obtainKrakenToken(input: {{ APIKey: "{api_key}" }}) {{
        token
    }}
}}"""

accept_terms_query = """mutation {{
    acceptTermsAndConditions(input: {{
        accountNumber: "{account_number}",
        enrolmentId: "{enrolment_id}",
        termsVersion: {{
            versionMajor: 1,
            versionMinor: 1
        }}
    }}) {{
        acceptedVersion
    }}
}}"""

consumption_query = """query {{
    smartMeterTelemetry(
        deviceId: "{device_id}"
        grouping: HALF_HOURLY
        start: "{start_date}"
        end: "{end_date}"
    ) {{
        readAt
        consumptionDelta
        costDeltaWithTax
    }}
}}"""

account_query = """query {{
    account(
        accountNumber: "{acc_number}"
    ) {{
        electricityAgreements(active: true) {{
            validFrom
            validTo
            meterPoint {{
                meters(includeInactive: false) {{
                    smartDevices {{
                        deviceId
                    }}
                }}
                mpan
            }}
            tariff {{
                ... on HalfHourlyTariff {{
                    id
                    productCode
                    tariffCode
                    productCode
                    standingCharge
                }}
            }}
        }}
    }}
}}"""

enrolment_query = """query {{
    productEnrolments(accountNumber: "{acc_number}") {{
        id
        status
        product {{
            code
            displayName
        }}
        stages {{
            name
            status
            steps {{
                displayName
                status
                updatedAt
            }}
        }}
    }}
}}"""

def send_message(content):
    """
    Sends a message to Discord and/or Telegram based on the config.py file if those variables are set.
    """
    print(content)  # Log the message regardless of where it's sent

    if hasattr(config, 'DISCORD_WEBHOOK') and config.DISCORD_WEBHOOK:
        content_discord = f"`{content}`"
        data = {"content": content_discord}
        try:
            response = requests.post(config.DISCORD_WEBHOOK, json=data)
            response.raise_for_status()  # Raise HTTPError for bad responses
        except requests.exceptions.RequestException as e:
            print(f"Error sending Discord message: {e}")

    if hasattr(config, 'TELEGRAM_BOT_TOKEN') and config.TELEGRAM_BOT_TOKEN and hasattr(config, 'TELEGRAM_CHAT_ID') and config.TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": content}
        try:
            response = requests.post(url, data=data)
            response.raise_for_status()  # Raise HTTPError for bad responses
        except requests.exceptions.RequestException as e:
            print(f"Error sending Telegram message: {e}")

def get_token():
    """Obtain an authentication token using the GraphQL API."""
    transport = AIOHTTPTransport(url=f"{config.BASE_URL}/graphql/")
    client = Client(transport=transport, fetch_schema_from_transport=True)
    query = gql(token_query.format(api_key=config.API_KEY))
    result = client.execute(query)
    return result['obtainKrakenToken']['token']

def setup_gql(token):
    """Set up the GraphQL client with the obtained token."""
    global gql_transport, gql_client
    gql_transport = AIOHTTPTransport(url=f"{config.BASE_URL}/graphql/", headers={'Authorization': f'{token}'})
    gql_client = Client(transport=gql_transport, fetch_schema_from_transport=True)

def rest_query(url):
    """Make a REST API request and return the response data."""
    response = requests.get(url)
    if response.ok:
        return response.json()
    else:
        raise Exception(f"ERROR: rest_query failed querying `{url}` with {response.status_code}")

def get_acc_info():
    """Retrieve account information, including the current tariff, standing charge, region code, and consumption data."""
    query = gql(account_query.format(acc_number=config.ACC_NUMBER))
    result = gql_client.execute(query)

    tariff_code = next(agreement['tariff']['tariffCode']
                       for agreement in result['account']['electricityAgreements']
                       if 'tariffCode' in agreement['tariff'])
    region_code = tariff_code[-1]
    device_id = next(device['deviceId']
                     for agreement in result['account']['electricityAgreements']
                     for meter in agreement['meterPoint']['meters']
                     for device in meter['smartDevices']
                     if 'deviceId' in device)
    curr_stdn_charge = next(agreement['tariff']['standingCharge']
                            for agreement in result['account']['electricityAgreements']
                            if 'standingCharge' in agreement['tariff'])

    current_tariff = None
    for tariff in potential_tariffs:
        if tariff in tariff_code:
            current_tariff = tariff
            break

    if current_tariff is None:
        raise Exception(f"ERROR: Unknown tariff code: {tariff_code}")

    # Get consumption for today
    result = gql_client.execute(
        gql(consumption_query.format(device_id=device_id, start_date=f"{date.today()}T00:00:00Z",
                                     end_date=f"{date.today()}T23:59:59Z")))
    consumption = result['smartMeterTelemetry']

    return current_tariff, curr_stdn_charge, region_code, consumption

def get_potential_tariff_rates(tariff, region_code):
    """Fetch potential tariff rates for a given tariff and region code."""
    all_products = rest_query(f"{config.BASE_URL}/products")
    tariff_code = next(
        product["code"] for product in all_products['results']
        if product['display_name'] == ("Agile Octopus" if tariff == "AGILE" else "Octopus Go" if tariff == "GO" else "Cosy Octopus")
        and product['direction'] == "IMPORT"
        and product['brand'] == "OCTOPUS_ENERGY"
    )
    product_code = f"E-1R-{tariff_code}-{region_code}"

    today = date.today()
    unit_rates = rest_query(
        f"{config.BASE_URL}/products/{tariff_code}/electricity-tariffs/{product_code}/standard-unit-rates/?period_from={today}T00:00:00Z&period_to={today}T23:59:59Z")
    standing_charge = rest_query(
        f"{config.BASE_URL}/products/{tariff_code}/electricity-tariffs/{product_code}/standing-charges/")

    return standing_charge['results'][0]['value_inc_vat'], unit_rates['results']

def calculate_potential_costs(consumption_data, rate_data):
    """Calculate potential costs based on consumption data and rate data."""
    period_costs = []
    for consumption in consumption_data:
        read_time = consumption['readAt'].replace('+00:00', 'Z')
        matching_rate = next(
            rate for rate in rate_data
            if rate['valid_from'] <= read_time <= rate['valid_to']
        )

        consumption_kwh = float(consumption['consumptionDelta']) / 1000
        cost = float("{:.4f}".format(consumption_kwh * matching_rate['value_inc_vat']))

        period_costs.append({
            'period_end': read_time,
            'consumption_kwh': consumption_kwh,
            'rate': matching_rate['value_inc_vat'],
            'calculated_cost': cost,
        })
    return period_costs

def accept_new_agreement():
    """Accept a new agreement if an enrolment is in progress or automatically completed."""
    query = gql(enrolment_query.format(acc_number=config.ACC_NUMBER))
    result = gql_client.execute(query)
    try:
        enrolment_id = next(entry['id'] for entry in result['productEnrolments'] if entry['status'] == "IN_PROGRESS")
    except StopIteration:
        today = datetime.now().date()
        for entry in result['productEnrolments']:
            for stage in entry['stages']:
                if stage['name'] == 'post-enrolment':
                    last_step_date = datetime.fromisoformat(
                        stage['steps'][-1]['updatedAt'].replace('Z', '+00:00')).date()
                    if last_step_date == today and stage['status'] == 'COMPLETED':
                        send_message("Post-enrolment automatically completed with today's date.")
                        return
        raise Exception("ERROR: No completed post-enrolment found today and no in-progress enrolment.")
    query = gql(accept_terms_query.format(account_number=config.ACC_NUMBER, enrolment_id=enrolment_id))
    gql_client.execute(query)

def verify_new_agreement():
    """Verify if a new agreement has been successfully accepted."""
    query = gql(account_query.format(acc_number=config.ACC_NUMBER))
    result = gql_client.execute(query)
    today = datetime.now().date()
    valid_from = next(datetime.fromisoformat(agreement['validFrom']).date()
                      for agreement in result['account']['electricityAgreements']
                      if 'validFrom' in agreement)
    return valid_from == today

def switch_tariff(target_tariff):
    """Use Playwright to automate the process of switching tariffs on the Octopus Energy website."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        page.goto("https://octopus.energy/")
        page.wait_for_timeout(1000)
        page.get_by_label("Log in to my account").click()
        page.wait_for_timeout(1000)
        page.get_by_placeholder("Email address").click()
        page.wait_for_timeout(1000)
        page.get_by_placeholder("Email address").fill(config.OCTOPUS_LOGIN_EMAIL)
        page.wait_for_timeout(1000)
        page.get_by_placeholder("Email address").press("Tab")
        page.wait_for_timeout(1000)
        page.get_by_placeholder("Password").fill(config.OCTOPUS_LOGIN_PASSWD)
        page.wait_for_timeout(1000)
        page.get_by_placeholder("Password").press("Enter")
        page.wait_for_timeout(1000)
        # Website is different for COSY tariff
        if target_tariff == "COSY":
            page.goto(f"https://octopus.energy/smart/cosy-octopus/sign-up/?accountNumber={config.ACC_NUMBER}")
        page.goto(f"https://octopus.energy/smart/{target_tariff.lower()}/sign-up/?accountNumber={config.ACC_NUMBER}")
        page.wait_for_timeout(10000)
        page.locator("section").filter(has_text="Already have a SMETS2 or “").get_by_role("button").click()
        page.wait_for_timeout(10000)
        context.close()
        browser.close()

def compare_and_switch():
    """Compare current and potential costs, and switch tariffs if the potential cost is lower."""
    send_message("Octobot on. Starting comparison of today's costs...")
    curr_tariff, curr_stdn_charge, region_code, consumption = get_acc_info()
    total_curr_cost = sum(float(entry['costDeltaWithTax']) for entry in consumption) + curr_stdn_charge
    send_message(f"Current cost on {curr_tariff}: £{total_curr_cost / 100:.2f}")
    best_tariff = curr_tariff
    best_cost = total_curr_cost

    for tariff in potential_tariffs:
        if tariff == curr_tariff:
            continue

        potential_std_charge, potential_unit_rates = get_potential_tariff_rates(tariff, region_code)
        potential_costs = calculate_potential_costs(consumption, potential_unit_rates)
        total_potential_calculated = sum(period['calculated_cost'] for period in potential_costs) + potential_std_charge
        send_message(f"Potential cost on {tariff}: £{total_potential_calculated / 100:.2f}")
        if total_potential_calculated < best_cost:
            best_tariff = tariff
            best_cost = total_potential_calculated

    summary = f"Best potential cost on {best_tariff}: £{best_cost / 100:.2f} vs your current cost on {curr_tariff}: £{total_curr_cost / 100:.2f}"
    if config.DRY_RUN:
        send_message("DRY RUN: " + summary)
    elif best_tariff != curr_tariff:
        send_message(summary + f"\nInitiating Switch to {best_tariff}")
        switch_tariff(best_tariff)
        send_message("Tariff switch requested successfully.")
        time.sleep(60)
        accept_new_agreement()
        send_message("Accepted agreement. Switch successful.")

        if verify_new_agreement():
            send_message("Verified new agreement successfully. Process finished.")
        else:
            send_message("Unable to accept new agreement. Please check your emails.")
    else:
        send_message("Not switching today. " + summary)

def run_tariff_compare():
    """Main function to run the tariff comparison and switching process."""
    try:
        setup_gql(get_token())
        if gql_transport and gql_client:
            compare_and_switch()
        else:
            raise Exception("ERROR: setup_gql has failed")
    except Exception:
        send_message(traceback.format_exc())

if __name__ == "__main__":
    run_tariff_compare()
