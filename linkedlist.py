from node import node

class linkedlist:
    def __init__(self):
        self.head = None

    def isEmpty(self)->bool:
        return self.head is None

    def addNewNode(self, anydata:any)->None:
        newnode = node(anydata)
        if self.isEmpty():
            self.head = newnode
        else:
            current = self.head
            while current.pointer is not None:
                current = current.pointer
            current.pointer = newnode

    def SeeAll(self)->None:
        if self.isEmpty():
            print("The list is empty")
        else:
            current = self.head
            while current is not None:
                print(current.data)
                current = current.pointer

    def getAll(self)->list:
        # Same walk as SeeAll, but returns the values so callers can use them
        items = []
        current = self.head
        while current is not None:
            items.append(current.data)
            current = current.pointer
        return items

    def count(self)->int:
        total = 0
        current = self.head
        while current is not None:
            total += 1
            current = current.pointer
        return total

    def search(self, target:any)->bool:
        current = self.head
        while current is not None:
            if current.data == target:
                return True
            current = current.pointer
        return False

    def makeEmpty(self)->None:
        # Dropping the head is enough: with nothing referencing the first node,
        # Python's garbage collector reclaims the whole chain
        self.head = None


newlist = linkedlist()
newlist.SeeAll()
print("Empty?", newlist.isEmpty())

newlist.addNewNode(3)
newlist.addNewNode(5)
newlist.addNewNode(8)
newlist.addNewNode(9)
newlist.SeeAll()

print("All nodes:", newlist.getAll())
print("Number of nodes:", newlist.count())
print("Find 8:", newlist.search(8))
print("Find 4:", newlist.search(4))
print("Empty?", newlist.isEmpty())

newlist.makeEmpty()
newlist.SeeAll()
print("Empty?", newlist.isEmpty())
print("Find 8 after emptying:", newlist.search(8))
