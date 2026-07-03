def map_schema_with_values(input_schema, input_value, batch_value):
    # Extract array names and non-array inputs from schema
    array_inputs = {}
    single_inputs = {}

    for item in input_schema:
        name = item["name"]
        value_ref = item["input"]["value"]["content"]

        if value_ref["name"] in batch_value:
            array_name = value_ref["name"]
            array_inputs[name] = array_name
        else:
            single_inputs[name] = value_ref["name"]

    # Get array lengths to determine number of combinations
    array_lengths = {name: len(batch_value[ref_name]) for name, ref_name in array_inputs.items()}

    if not array_lengths:
        return []

    # Use the shortest array length as the base length
    min_length = min(array_lengths.values())

    # Generate combinations
    result = []

    for i in range(min_length):
        combination = {}

        # Add array values
        for output_name, array_name in array_inputs.items():
            combination[output_name] = batch_value[array_name][i]

        # Add single input values
        for output_name, input_name in single_inputs.items():
            combination[output_name] = input_value[input_name]

        result.append(combination)

    return result
