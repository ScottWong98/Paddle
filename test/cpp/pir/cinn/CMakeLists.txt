if(WITH_TESTING AND WITH_CINN)
  cc_test_old(
    test_new_ir_compiler
    SRCS
    new_ir_compiler_test.cc
    DEPS
    new_ir_compiler
    convert_to_dialect
    cinn_runtime_dialect
    pir
    phi
    gtest
    glog)
  set_tests_properties(test_new_ir_compiler PROPERTIES LABELS "RUN_TYPE=CINN")

  cc_test_old(
    test_jit_instruction
    SRCS
    jit_instruction_test.cc
    DEPS
    interpreter
    new_ir_compiler
    convert_to_dialect)
  set_tests_properties(test_jit_instruction PROPERTIES LABELS "RUN_TYPE=CINN")

  paddle_test(test_group_op SRCS group_op_test.cc DEPS cinn_op_dialect)
  set_tests_properties(test_group_op PROPERTIES LABELS "RUN_TYPE=CINN")

  paddle_test(test_pir_build_cinn_pass SRCS build_cinn_pass_test.cc DEPS
              pd_build_cinn_pass)
  set_tests_properties(test_pir_build_cinn_pass PROPERTIES LABELS
                                                           "RUN_TYPE=CINN")
endif()
