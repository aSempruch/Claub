# state:
# map that has a pointer to each node
# doubly linked list with root and tail dummy nodes to make pointer logic easier

class LRUCache:

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.size = 0
        self.lookup = dict()
        self.head = Node(None)
        self.tail = Node(None)

        self.head.next = self.tail
        self.tail.prev = self.head

    def get(self, key: int) -> int:
        if key not in self.lookup:
            return -1

        target = self.lookup[key]
        self
        

    def put(self, key: int, value: int) -> None:
        
class Node:

    def __init__(self, val: int):
        self.val = val
        self.prev = None
        self.next = None

# Your LRUCache object will be instantiated and called as such:
# obj = LRUCache(capacity)
# param_1 = obj.get(key)
# obj.put(key,value)