def cross_reference(followers_list, following_list):
    """Split two username lists into (not following back, fans, mutuals)."""
    followers = set(followers_list)
    following = set(following_list)

    not_following_back = sorted(following - followers, key=str.casefold)
    fans = sorted(followers - following, key=str.casefold)
    mutuals = sorted(followers & following, key=str.casefold)

    return not_following_back, fans, mutuals
