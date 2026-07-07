from duckduckgo_search import DDGS
print("Testing individual...")
r1 = DDGS().text('"Observe.AI" site:instahyre.com', max_results=2)
print(r1)

print("\nTesting OR...")
r2 = DDGS().text('"Observe.AI" site:instahyre.com OR site:cutshort.io', max_results=2)
print(r2)
