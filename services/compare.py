def compare_followers(old_list, new_list):

    old = set(old_list)
    new = set(new_list)

    gained = list(new - old)
    lost = list(old - new)

    return gained, lost

def cross_reference(followers_list, following_list):

    followers = set(followers_list)
    following = set(following_list)

    not_following_back = list(following - followers)
    fans = list(followers - following)
    mutuals = list(followers.intersection(following)) 

    return not_following_back, fans, mutuals