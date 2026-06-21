def reverse_text():
    # Take user input as text
    text = input("Enter the text you want to reverse: ")
    
    # Reverse the string using slicing
    reversed_text = text[::-1]
    
    # Print the result
    print(f"Reversed text: {reversed_text}")

if __name__ == "__main__":
    reverse_text()
