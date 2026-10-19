from node import node

class queue:
    # FIFO: items join at the tail and leave from the head
    def __init__(self):
        self.head = None
        # Keeping a tail reference makes enqueue O(1); without it every
        # enqueue would have to walk the whole chain like addNewNode does
        self.tail = None
        self.size = 0

    def isEmpty(self)->bool:
        return self.head is None

    def enqueue(self, anydata:any)->None:
        newnode = node(anydata)
        if self.isEmpty():
            self.head = newnode
        else:
            self.tail.pointer = newnode
        self.tail = newnode
        self.size += 1

    def dequeue(self)->any:
        if self.isEmpty():
            raise IndexError("dequeue from an empty queue")
        removed = self.head
        self.head = removed.pointer
        # Last item gone: the tail would otherwise still point at it
        if self.head is None:
            self.tail = None
        self.size -= 1
        return removed.data

    def peek(self)->any:
        if self.isEmpty():
            raise IndexError("peek at an empty queue")
        return self.head.data

    def count(self)->int:
        return self.size

    def getAll(self)->list:
        # Front of the queue first, i.e. the order items will be dequeued
        items = []
        current = self.head
        while current is not None:
            items.append(current.data)
            current = current.pointer
        return items

    def SeeAll(self)->None:
        if self.isEmpty():
            print("The queue is empty")
        else:
            for item in self.getAll():
                print(item)

    def makeEmpty(self)->None:
        self.head = None
        self.tail = None
        self.size = 0


if __name__ == "__main__":
    newqueue = queue()
    newqueue.SeeAll()
    print("Empty?", newqueue.isEmpty())

    newqueue.enqueue(3)
    newqueue.enqueue(5)
    newqueue.enqueue(8)
    newqueue.enqueue(9)
    newqueue.SeeAll()

    print("All items:", newqueue.getAll())
    print("Number of items:", newqueue.count())
    print("Front:", newqueue.peek())

    print("Dequeued:", newqueue.dequeue())
    print("Dequeued:", newqueue.dequeue())
    print("Remaining:", newqueue.getAll())

    newqueue.enqueue(12)
    print("After enqueue 12:", newqueue.getAll())

    newqueue.makeEmpty()
    newqueue.SeeAll()
    print("Empty?", newqueue.isEmpty())

    try:
        newqueue.dequeue()
    except IndexError as error:
        print("Error:", error)
