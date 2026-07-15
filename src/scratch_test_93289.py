from popcorn_box import api
import sys
import logging
logging.basicConfig(level=logging.DEBUG)

print("Fetching details for tmdb:series:93289...")
details = api.fetch_movie_details("tmdb:series:93289", "series", title="Reality")
print("Result details:", details)
