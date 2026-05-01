# 📌 Fluxo Atual de Geração de Projetos Agrícolas (AS-IS)

## 🧠 Visão Geral

Este documento descreve o fluxo atual utilizado para geração de projetos de piloto automático agrícola a partir de linhas de VANT, utilizando AutoCAD + plugin AgroCAD.

---

## 🔹 1. Entrada de Dados

**Origem:**
- Linhas provenientes de voo de VANT
- Formato: `.shp`
- Separação por talhão via layer/código

---

## 🔹 2. Importação no AutoCAD

### Comando: MAPIMPORT


### Etapas:
1. Selecionar arquivos `.shp`
2. Confirmar importação
3. Definir/verificar layers

### Resultado:
- Linhas carregadas no ambiente do AutoCAD

---

## 🔹 3. Tratamento Geométrico (AgroCAD)

### 🔸 3.1 SMP (Simplificação de Linhas)

**Comando: SMP


**Processo:**
1. Selecionar todas as linhas
2. Inserir valor de simplificação

**Valores utilizados:**
- Entre `0.01` e `0.05`

**Resultado:**
- Redução de vértices mantendo a geometria

---

### 🔸 3.2 HME (Padronização de Vértices)

**Comando: HME


**Processo:**
1. Selecionar todas as linhas
2. Inserir valor mínimo → `5`
3. Inserir valor máximo → `15`

**Resultado:**
- Distribuição uniforme de vértices nas linhas

---

## 🔹 4. Classificação (AgroCAD)

### Comando: KLA


### Etapas:

1. Preencher campos:

| Campo   | Valor |
|--------|------|
| Cliente | US PEDRA (fixo) |
| Fazenda | Código + tipo (ex: 10073_1L) |
| Talhão  | Parte 1 |

---

2. Clicar no botão `+` (linha vermelha)

3. Selecionar as linhas no desenho

4. Sistema calcula o tamanho do projeto

---

### 🔸 Regra de Decisão

- Se `< 2850 kb`:
  - Prosseguir normalmente

- Se `> 2850 kb`:
  - Dividir projeto em partes
  - Processo manual obrigatório

---

### Resultado:
- Projeto classificado (ou parcialmente classificado)

---

## 🔹 5. Exportação (AgroCAD)

### Comando: GOEXP


### Etapas:

1. Ativar:
   - ☑ Multi Export
   - ☑ John Deere
   - ☑ PTX Trimble

2. Definir pasta destino

3. Clicar em: Exportar


---

### Resultado:
- Arquivos gerados para sistemas de piloto automático

---

## 🔹 6. Organização Final

Processos realizados:

- Separação por fazenda
- Separação por tipo:
  - 1L (linha)
  - 2L (entrelinha)
- Disponibilização para gestores

---

# 🚨 Problemas Identificados

## ❗ 1. Autenticação do AgroCAD
- Necessidade de login frequente (~30x/dia)
- Impede automação contínua

---

## ❗ 2. MAPIMPORT
- Interface gráfica obrigatória
- Não automatizável diretamente

---

## ❗ 3. KLA
- Interface manual
- Requer decisão humana (divisão por tamanho)

---

## ❗ 4. GOEXP
- Totalmente dependente de interface gráfica

---

## ❗ 5. Retrabalho
- Alteração em talhão exige refazer projeto completo

---

## ❗ 6. Tempo Operacional
- Processo manual
- Repetitivo e sujeito a erro humano

---

# 🔄 Resumo do Fluxo

SHP (VANT)
↓
MAPIMPORT (manual)
↓
SMP (manual)
↓
HME (manual)
↓
KLA (manual + decisão)
↓
GOEXP (manual)
↓
Arquivos finais



---

# 🎯 Objetivo Futuro

Automatizar o máximo possível deste fluxo, reduzindo:

- Intervenção manual
- Tempo de execução
- Dependência de interface gráfica
- Impacto de autenticações recorrentes

---




