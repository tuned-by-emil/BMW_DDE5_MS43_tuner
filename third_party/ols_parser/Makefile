CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -std=c99 -D_POSIX_C_SOURCE=200809L
EXEEXT  ?=

TARGET = ols_dump$(EXEEXT)
OBJS   = ols_dump.o ols.o

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $^

ols.o:      ols.c ols.h
ols_dump.o: ols_dump.c ols.h

clean:
	rm -f *.o *.obj ols_dump ols_dump.exe

.PHONY: all clean
