import sys
sys.path.insert(0, 'src/popcorn_box')
import api
import database
database.init_db()
print(api.fetch_movie_details("tmdb:series:234613", "series"))
