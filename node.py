class node:
    def __init__(self, anydata:any)->None:
        self.data = anydata
        self.pointer = None  # next node in the list; None marks the tail
