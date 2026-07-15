from popcorn_box import api
import json
print(json.dumps(api.fetch_movie_details("tmdb:series:234613", "series"), indent=2))
