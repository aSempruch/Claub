# state:
# map that has a pointer to each node
# doubly linked list with root and tail dummy nodes to make pointer logic easier

class LRUCache:

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.size = 0

    def get(self, key: int) -> int:
        

    def put(self, key: int, value: int) -> None:
        
class Node:

    def __init__(self, val: int):
        self.val = val

# Your LRUCache object will be instantiated and called as such:
# obj = LRUCache(capacity)
# param_1 = obj.get(key)
# obj.put(key,value)