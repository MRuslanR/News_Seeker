# News_Seeker

This project is an automated news monitoring bot designed to track, aggregate, and summarize news from European sources via RSS feeds.

The system fetches news articles, filters out duplicates, uses AI (via OpenRouter/Gemini) to generate concise summaries for specific countries, compiles a final digest, and distributes it to a specified Telegram channel. It is fully configurable via an Excel spreadsheet and supports custom scheduling compliant with local timezones.

## Key Features

- **RSS Aggregation:** Multi-threaded fetching of RSS feeds defined by country.
- **AI Integration:** Uses OpenRouter (Google Gemini models) to filter noise, summarize articles, and generate a final coherent report.
- **Duplicate Detection:** storage of processed links in a SQLite database to prevent repetitive news.
- **Flexible Scheduling:** Tasks can be scheduled to run at specific times defined in the configuration file, respecting the configured timezone.
- **Remote Configuration:** Administrators can retrieve and update the configuration Excel file directly through Telegram commands.
- **Error Handling:** Automatic temporary disabling of broken RSS feeds with logging to the configuration file for review.

## Configuration

Before running the application (either locally or via Docker), you must configure the environment variables.

1. Rename the example file:
   cp .env.example .env

2. Open .env and populate the variables:
   - TELEGRAM_BOT_TOKEN: Your Telegram Bot Token.
   - TELEGRAM_CHAT_ID: The target chat/channel ID for reports.
   - OPENROUTER_API_KEY: Your API key for AI processing.

## Installation and Usage

You can run the project using Docker (recommended) or a local Python environment.

### Option 1: Docker (Recommended)

Prerequisites: Docker and Docker Compose installed.

1. Build and start the container:
   docker-compose up -d --build

2. View logs:
   docker-compose logs -f

3. Stop the container:
   docker-compose down

### Option 2: Manual Installation

Prerequisites: Python 3.8+

1. Create a virtual environment:
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\\Scripts\\activate

2. Install dependencies:
   pip install -r requirements.txt

3. Run the bot:
   python bot.py

## Excel Configuration (config.xlsx)

The file `config.xlsx` controls the sources and schedule. It must be present in the root directory (or mapped via Docker volumes).

**Sheet: Feeds**
Defines the RSS sources.
- Column A: `country_code` (e.g., FR, DE, UK).
- Columns B, C, etc.: RSS URLs.

**Sheet: Schedule**
Defines when the bot runs.
- Column A: `run_time_utc` (Format: HH:MM).
*Note: Despite the column name, the bot interprets these times according to the TIMEZONE variable set in the .env file.*

**Sheet: TempFailures / DisabledFeeds**
These sheets are automatically generated and updated by the bot to report broken links or feed errors.

## Telegram Commands

- `/start` - Manually triggers a news processing cycle immediately.
- `/get_excel` - Downloads the current `config.xlsx` file from the server.
- `/update_excel` - Upload a new `config.xlsx` file. Send the file with this command as the caption to update feeds and schedules without restarting the container.
