class Twitter:

    def __init__(self):
        self.follows = defaultdict(set)
        self.tweets = defaultdict(list)
        self.timestamp = 0

    def postTweet(self, userId: int, tweetId: int) -> None:
        self.tweets[userId].append(
            Tweet(tweetId, timestamp)
        )
        self.timestamp += 1

    def getNewsFeed(self, userId: int) -> List[int]:
        tweets = [*self.tweets[userId], *[self.tweets[f] for f in self.follows[userId]]]

    def follow(self, followerId: int, followeeId: int) -> None:
        self.follows[followerId].add(followeeId)

    def unfollow(self, followerId: int, followeeId: int) -> None:
        self.follows[followerId].remove(followeeId)

class Tweet:

    def __init__(self, id: int, timestamp: int):
        self.id = id
        self.timestamp = timestamp

# Your Twitter object will be instantiated and called as such:
# obj = Twitter()
# obj.postTweet(userId,tweetId)
# param_2 = obj.getNewsFeed(userId)
# obj.follow(followerId,followeeId)
# obj.unfollow(followerId,followeeId)