from node import node

class queue:
    def __init__(self):
        self.head = None
        self.tail = None  # lets enqueue skip walking the chain

    def isEmpty(self)->bool:
        return self.head is None

    def enqueue(self, anydata:any)->None:
        newnode = node(anydata)
        if self.isEmpty():
            self.head = newnode
        else:
            self.tail.pointer = newnode
        self.tail = newnode

    def dequeue(self)->any:
        if self.isEmpty():
            raise IndexError("queue is empty")
        data = self.head.data
        self.head = self.head.pointer
        if self.head is None:
            self.tail = None
        return data

    def peek(self)->any:
        if self.isEmpty():
            raise IndexError("queue is empty")
        return self.head.data


if __name__ == "__main__":
    q = queue()
    for n in (3, 5, 8):
        q.enqueue(n)
    print("Front:", q.peek())
    print("Dequeued:", q.dequeue(), q.dequeue(), q.dequeue())
    print("Empty?", q.isEmpty())
