from models.follower import Follower
from database import db
from datetime import datetime

def save_followers(kind, new_data_list):

    if not new_data_list:
        return [], [] 

   
    dates = [item['date'] for item in new_data_list if item['date']]
    if dates:
        min_date = min(dates)
    else:
      
        min_date = datetime(2000, 1, 1)

    existing_query = Follower.query.filter(
        Follower.kind == kind, 
        Follower.date_added >= min_date
    ).all()
    
    existing_map = {f.username: f for f in existing_query}
    new_usernames_set = {item['username'] for item in new_data_list}

    gained = []
    lost = []

    for item in new_data_list:
        username = item['username']
        user_date = item['date']
    
        global_exists = Follower.query.filter_by(username=username, kind=kind).first()
        
        if not global_exists:

            new_follower = Follower(username=username, kind=kind, date_added=user_date)
            db.session.add(new_follower)
            gained.append(username)
        elif global_exists and global_exists.date_added < min_date:
           
            pass


    for username, follower_obj in existing_map.items():
        if username not in new_usernames_set:
           
            db.session.delete(follower_obj)
            lost.append(username)

    db.session.commit()
    
    return gained, lost

def load_followers(kind):
    """Retorna lista completa para contagem total"""
    return [f.username for f in Follower.query.filter_by(kind=kind).all()]