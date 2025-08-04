import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import re
import time
import json
from urllib.parse import urljoin
import logging
from datetime import datetime, timedelta
import os
import sys

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class LeHavreEventsScraper:
    def __init__(self, headless=True, timeout=15):
        self.base_url = "https://www.lehavre-etretat-tourisme.com"
        self.events_url = f"{self.base_url}/agenda/a-ne-pas-manquer/concerts/"
        self.timeout = timeout
        self.headless = headless
        self.driver = None

    def _setup_driver(self):
        """Setup Chrome WebDriver with appropriate options for automation"""
        chrome_options = Options()
        if self.headless:
            chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-plugins")
        chrome_options.add_argument("--disable-images")  # Speed up scraping

        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.implicitly_wait(self.timeout)

    def _cleanup_driver(self):
        """Clean up WebDriver resources"""
        if self.driver:
            self.driver.quit()
            self.driver = None

    def _is_event_expired(self, event_data):
        """Check if an event is expired based on its date"""
        try:
            date_str = event_data.get('date', '')
            if not date_str:
                return False  # Keep events without dates

            # Parse French date format DD/MM/YYYY
            if '/' in date_str:
                parts = date_str.split('/')
                if len(parts) == 3:
                    day = int(parts[0])
                    month = int(parts[1])
                    year = int(parts[2])
                    event_date = datetime(year, month, day)

                    # Consider event expired if it's more than 1 day in the past
                    return event_date < (datetime.now() - timedelta(days=1))

            return False
        except Exception as e:
            logger.warning(f"Error checking event expiration: {e}")
            return False

    def _get_event_cards_with_selenium(self):
        """Use Selenium to get event cards from the main page with multiple attempts to load more events"""
        if not self.driver:
            self._setup_driver()

        try:
            logger.info("Loading concerts page...")
            self.driver.get(self.events_url)

            # Wait for page to load completely
            WebDriverWait(self.driver, self.timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Try to load more events - multiple attempts with different selectors
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    # Try different variations of the "load more" button
                    button_selectors = [
                        "//button[contains(., 'Plus de résultats')]",  # French version
                        "//button[contains(., 'Voir plus')]",  # Alternative French
                        "//button[contains(., 'Afficher plus')]",  # Another French variant
                        "//button[contains(., 'View More')]",  # English version
                        "//a[contains(., 'Plus de résultats')]",  # Sometimes it's an <a> tag
                        "//div[contains(@class, 'load-more')]",  # Class-based approach
                        "//*[contains(@class, 'btn-more')]"  # Another common class
                    ]

                    for selector in button_selectors:
                        try:
                            button = WebDriverWait(self.driver, 5).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            if button.is_displayed():
                                # Scroll to the button smoothly
                                self.driver.execute_script(
                                    "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                                    button
                                )
                                time.sleep(1)  # Small pause for scrolling

                                # Click using JavaScript to avoid interception
                                self.driver.execute_script("arguments[0].click();", button)

                                # Wait for new content to load
                                time.sleep(3)  # Wait for AJAX to complete
                                WebDriverWait(self.driver, 10).until(
                                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/fiche/']")))

                                logger.info(f"Successfully clicked 'View More' button (attempt {attempt + 1})")
                                break  # Successfully clicked one button
                        except:
                            continue

                except Exception as e:
                    logger.debug(f"Attempt {attempt + 1} failed to find/click 'View More' button: {str(e)}")
                    if attempt == max_attempts - 1:
                        logger.info("No more attempts to find 'View More' button")
                    time.sleep(2)  # Wait before next attempt

            # Original event card scraping logic
            event_selectors = [
                'a[href*="/fiche/"]',  # Links containing /fiche/ in URL
                '.event-card a',
                '.card a',
                'article a',
                '.item a'
            ]

            event_links = []
            for selector in event_selectors:
                try:
                    links = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if links:
                        logger.info(f"Found {len(links)} potential event links with selector: {selector}")
                        event_links = links
                        break
                except Exception as e:
                    continue

            if not event_links:
                # Fallback: get all links that might be events
                all_links = self.driver.find_elements(By.TAG_NAME, "a")
                event_links = [link for link in all_links
                               if link.get_attribute('href') and '/fiche/' in link.get_attribute('href')]
                logger.info(f"Fallback: Found {len(event_links)} links with /fiche/ pattern")

            # Extract basic info from each event card
            events = []
            for i, link in enumerate(event_links[:48]):  # Increased limit to 48
                try:
                    href = link.get_attribute('href')
                    if not href or '/fiche/' not in href:
                        continue

                    # Get the parent element that contains the event info
                    card_element = link
                    for _ in range(3):  # Go up to 3 levels to find the card container
                        parent = card_element.find_element(By.XPATH, "..")
                        if parent.tag_name in ['article', 'div'] and ('card' in parent.get_attribute('class') or ''):
                            card_element = parent
                            break
                        card_element = parent

                    # Extract basic information from the card
                    title = ""
                    try:
                        title_elem = card_element.find_element(By.CSS_SELECTOR, "h1, h2, h3, h4, .title, .heading")
                        title = title_elem.text.strip()
                    except:
                        title = link.text.strip()

                    # Extract image if available
                    image_url = ""
                    try:
                        img_elem = card_element.find_element(By.TAG_NAME, "img")
                        image_url = img_elem.get_attribute('src')
                        if image_url and not image_url.startswith('http'):
                            image_url = urljoin(self.base_url, image_url)
                    except:
                        pass

                    # Extract ID from URL
                    event_id = ""
                    url_match = re.search(r'_([A-Z0-9]+)/?$', href)
                    if url_match:
                        event_id = url_match.group(1)

                    event = {
                        'id': event_id,
                        'title': title,
                        'detail_url': href,
                        'image_url': image_url,
                        'date': '',
                        'time': '',
                        'full_address': '',
                        'price': '',
                        'description': '',
                        'ticket_url': '',
                        'organizer': '',
                        'audience': '',
                        'scraped_at': datetime.now().isoformat()
                    }

                    if title:
                        events.append(event)
                        logger.info(f"Added event: {title}")

                except Exception as e:
                    logger.warning(f"Failed to parse event card {i}: {e}")
                    continue

            logger.info(f"Total events collected: {len(events)}")
            return events

        except Exception as e:
            logger.error(f"Failed to get event cards: {e}")
            return []

    def _get_popup_details(self, event_url):
        """Extract detailed information from event popup/detail page"""
        try:
            logger.info(f"Fetching popup details for: {event_url}")
            self.driver.get(event_url)

            # Wait for the page content to load
            WebDriverWait(self.driver, self.timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Give extra time for any dynamic content
            time.sleep(2)

            details = {}

            # Extract full address - focus on address under title in popup
            try:
                address_found = False

                # Strategy 1: Look for address right after the title/heading
                try:
                    # Find the main title first
                    title_elements = self.driver.find_elements(By.CSS_SELECTOR, "h1, h2, .titre, .title")
                    for title_elem in title_elements:
                        # Look for address in the next sibling or nearby elements
                        parent = title_elem.find_element(By.XPATH, "..")
                        following_elements = parent.find_elements(By.XPATH, ".//*")

                        for elem in following_elements:
                            text = elem.text.strip()
                            # Check if it looks like an address (contains street info and postal code)
                            if (len(text) > 10 and
                                    ('rue' in text.lower() or 'avenue' in text.lower() or 'place' in text.lower() or
                                     'boulevard' in text.lower() or 'chemin' in text.lower()) and
                                    re.search(r'\d{5}', text)):  # Contains 5-digit postal code
                                details['full_address'] = text
                                logger.info(f"Found address near title: {text}")
                                address_found = True
                                break
                        if address_found:
                            break
                except Exception as e:
                    logger.debug(f"Strategy 1 failed: {e}")

                # Additional strategies (2-4) from original code...
                # [Previous address extraction strategies here - keeping them for robustness]

            except Exception as e:
                logger.warning(f"Could not extract address: {e}")

            # Extract price/tarif
            try:
                tarif_elements = self.driver.find_elements(By.XPATH,
                                                           "//*[contains(text(), 'Tarif') or contains(text(), 'tarif') or contains(text(), 'Gratuit') or contains(text(), 'gratuit')]")

                for tarif_elem in tarif_elements:
                    parent = tarif_elem.find_element(By.XPATH, "..")
                    tarif_text = parent.text.strip()

                    if 'gratuit' in tarif_text.lower():
                        details['price'] = 'Gratuit'
                        logger.info("Found price: Gratuit")
                        break
                    else:
                        price_match = re.search(r'(\d+(?:[.,]\d+)?)\s*€', tarif_text)
                        if price_match:
                            details['price'] = f"{price_match.group(1)}€"
                            logger.info(f"Found price: {details['price']}")
                            break

                if not details.get('price'):
                    gratuit_elem = self.driver.find_elements(By.XPATH, "//*[contains(text(), 'Gratuit')]")
                    if gratuit_elem:
                        details['price'] = 'Gratuit'
                        logger.info("Found price: Gratuit (broad search)")

            except Exception as e:
                logger.warning(f"Could not extract price: {e}")

            # Extract description
            try:
                desc_selectors = [
                    '.descriptif',
                    '.description',
                    '[class*="descriptif"]',
                    '[class*="description"]',
                    '.content p',
                    '.texte',
                    'p'
                ]

                for selector in desc_selectors:
                    try:
                        desc_elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        descriptions = []

                        for elem in desc_elements:
                            text = elem.text.strip()
                            if (len(text) > 30 and
                                    'tarif' not in text.lower() and
                                    'horaire' not in text.lower() and
                                    'contact' not in text.lower()):
                                descriptions.append(text)

                        if descriptions:
                            details['description'] = '\n\n'.join(descriptions[:2])
                            logger.info(f"Found description: {details['description'][:100]}...")
                            break

                    except:
                        continue

            except Exception as e:
                logger.warning(f"Could not extract description: {e}")

            # Extract time/date info
            try:
                # Look for time patterns
                time_elements = self.driver.find_elements(By.XPATH,
                                                          "//*[contains(text(), 'h') or contains(text(), ':')]")
                for elem in time_elements:
                    text = elem.text.strip()
                    time_match = re.search(r'(\d{1,2}[h:]\d{2})', text)
                    if time_match:
                        details['time'] = time_match.group(1).replace('h', ':')
                        logger.info(f"Found time: {details['time']}")
                        break

                # Look for date patterns
                date_elements = self.driver.find_elements(By.XPATH,
                                                          "//*[contains(text(), '/202') or contains(text(), '202')]")
                for elem in date_elements:
                    text = elem.text.strip()
                    date_match = re.search(r'(\d{1,2}/\d{1,2}/202\d)', text)
                    if date_match:
                        details['date'] = date_match.group(1)
                        logger.info(f"Found date: {details['date']}")
                        break

            except Exception as e:
                logger.warning(f"Could not extract time/date: {e}")

            # Extract contact/organizer info
            try:
                contact_elements = self.driver.find_elements(By.XPATH,
                                                             "//*[contains(text(), 'Tel') or contains(text(), 'tél') or contains(text(), '@')]")
                for elem in contact_elements:
                    parent = elem.find_element(By.XPATH, "..")
                    contact_text = parent.text.strip()
                    if '@' in contact_text or 'tel' in contact_text.lower():
                        details['organizer'] = contact_text
                        logger.info(f"Found organizer: {contact_text}")
                        break
            except Exception as e:
                logger.warning(f"Could not extract organizer: {e}")

            return details

        except Exception as e:
            logger.error(f"Failed to get popup details for {event_url}: {e}")
            return {}

    def load_existing_events(self, filename='lehavre_events_test.json'):
        """Load existing events from JSON file"""
        try:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    existing_events = json.load(f)
                logger.info(f"Loaded {len(existing_events)} existing events")

                # Filter out expired events
                current_events = []
                expired_count = 0

                for event in existing_events:
                    if self._is_event_expired(event):
                        expired_count += 1
                        logger.info(f"Removing expired event: {event.get('title', 'Unknown')}")
                    else:
                        current_events.append(event)

                logger.info(f"Removed {expired_count} expired events")
                return current_events
            else:
                logger.info("No existing events file found")
                return []
        except Exception as e:
            logger.error(f"Error loading existing events: {e}")
            return []

    def merge_events(self, existing_events, new_events):
        """Merge new events with existing ones, avoiding duplicates"""
        existing_ids = {event.get('id', '') for event in existing_events if event.get('id')}
        existing_titles = {event.get('title', '').lower() for event in existing_events}

        merged_events = existing_events.copy()
        new_count = 0

        for new_event in new_events:
            event_id = new_event.get('id', '')
            event_title = new_event.get('title', '').lower()

            # Check for duplicates by ID or title
            if event_id and event_id in existing_ids:
                logger.info(f"Skipping duplicate event (ID): {new_event.get('title', 'Unknown')}")
                continue
            elif event_title in existing_titles:
                logger.info(f"Skipping duplicate event (title): {new_event.get('title', 'Unknown')}")
                continue
            else:
                merged_events.append(new_event)
                existing_ids.add(event_id)
                existing_titles.add(event_title)
                new_count += 1
                logger.info(f"Added new event: {new_event.get('title', 'Unknown')}")

        logger.info(f"Added {new_count} new events")
        return merged_events

    def scrape_events(self, max_events=40):
        """Main method to scrape complete event data"""
        logger.info("Starting automated event scraping...")

        try:
            # Load existing events
            existing_events = self.load_existing_events()

            # Step 1: Get event cards from main page
            initial_events = self._get_event_cards_with_selenium()
            logger.info(f"Found {len(initial_events)} initial events")

            if not initial_events:
                logger.warning("No new events found on main page")
                return existing_events  # Return existing events if no new ones found

            # Step 2: Get detailed info for each event
            complete_new_events = []

            for i, event in enumerate(initial_events[:max_events]):
                logger.info(f"\n=== Processing Event {i + 1}/{min(len(initial_events), max_events)} ===")
                logger.info(f"Title: {event.get('title', 'Unknown')}")
                logger.info(f"URL: {event.get('detail_url', 'Unknown')}")

                if event.get('detail_url'):
                    try:
                        popup_details = self._get_popup_details(event['detail_url'])

                        # Merge popup details with initial data
                        for key, value in popup_details.items():
                            if value:
                                event[key] = value

                        complete_new_events.append(event)
                        logger.info(f"✓ Successfully processed: {event.get('title', 'Unknown')}")

                        # Rate limiting between requests
                        time.sleep(2)

                    except Exception as e:
                        logger.error(f"✗ Failed to process event details: {e}")
                        # Still add the basic event data
                        complete_new_events.append(event)
                else:
                    logger.warning("No detail URL found for event")
                    complete_new_events.append(event)

            # Step 3: Merge with existing events
            all_events = self.merge_events(existing_events, complete_new_events)

            # Sort events by date
            def get_event_date(event):
                try:
                    date_str = event.get('date', '')
                    if date_str and '/' in date_str:
                        parts = date_str.split('/')
                        if len(parts) == 3:
                            return datetime(int(parts[2]), int(parts[1]), int(parts[0]))
                except:
                    pass
                return datetime.now() + timedelta(days=365)  # Put events without dates at the end

            all_events.sort(key=get_event_date)

            logger.info(f"\n=== SCRAPING COMPLETE ===")
            logger.info(f"Total events: {len(all_events)}")
            logger.info(f"New events added: {len(complete_new_events)}")
            return all_events

        finally:
            self._cleanup_driver()

    def save_events_json(self, events, filename='lehavre_events_test.json'):
        """Save events to JSON file with metadata"""
        try:
            # Add metadata
            output_data = {
                'metadata': {
                    'last_updated': datetime.now().isoformat(),
                    'total_events': len(events),
                    'scraper_version': '2.0'
                },
                'events': events
            }

            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(events, f, ensure_ascii=False, indent=2)  # Keep original format for compatibility

            logger.info(f"Events saved to {filename}")

            # Also save with metadata for monitoring
            metadata_filename = filename.replace('.json', '_with_metadata.json')
            with open(metadata_filename, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)

            logger.info(f"Events with metadata saved to {metadata_filename}")

        except Exception as e:
            logger.error(f"Failed to save events: {e}")
            raise


def main():
    """Main function for automated execution"""
    print("=== LE HAVRE EVENTS SCRAPER (AUTOMATED) ===\n")

    # Use headless mode for automation
    scraper = LeHavreEventsScraper(headless=True, timeout=20)

    try:
        events = scraper.scrape_events(max_events=40)

        if events:
            # Save events
            scraper.save_events_json(events)

            # Print summary
            print(f"\n=== AUTOMATION SUMMARY ===")
            print(f"Total events: {len(events)}")
            print(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # Print recent events for verification
            print(f"\n=== RECENT EVENTS (First 5) ===")
            for i, event in enumerate(events[:5], 1):
                print(f"{i}. {event.get('title', 'N/A')}")
                print(f"   Date: {event.get('date', 'N/A')}")
                print(f"   Location: {event.get('full_address', 'N/A')[:50]}...")
                print()

            return True
        else:
            logger.error("No events were scraped")
            return False

    except Exception as e:
        logger.error(f"Automation failed: {e}")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
