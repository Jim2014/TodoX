import sys
import os

# Add the current directory to path so we can import the module
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from gemini_cli_agent_main import parse_args, MODEL_ALIASES
import shlex

def test_parse_args():
    # Test cases
    cases = [
        # Explicit m flag
        ("-m gemini-custom -o 10", 'gemini-custom'),
        # New model flags
        ("-3p -o 5", 'gemini-3-pro-preview'),
        ("-3f", 'gemini-3-flash-preview'),
        ("-2.5p -msg hello", 'gemini-2.5-pro'),
        ("-2.5f", 'gemini-2.5-flash'),
        ("-3.1p", 'gemini-3.1-pro-preview'),
        ("-3.1f", 'gemini-3.1-flash-lite-preview'),
        # Default fallback
        ("-o 15 -msg nothing", '')
    ]
    
    for arg_str, expected_model in cases:
        args = shlex.split(arg_str)
        parsed_args = parse_args(args)
        
        model = ''
        if 'm' in parsed_args and isinstance(parsed_args['m'], str):
            model = parsed_args['m']
        else:
            for alias, model_name in MODEL_ALIASES.items():
                if alias in parsed_args:
                    model = model_name
                    break
        
        assert model == expected_model, f"Failed for {arg_str}: got {model} expected {expected_model}"
        print(f"Passed: {arg_str} -> {model if model else 'default'}")

if __name__ == "__main__":
    test_parse_args()
    print("All tests passed.")
